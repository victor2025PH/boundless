"""hub 引擎目录离线哨兵门禁（2026-08-22「fish 冒充 IndexTTS-2」事故的零流量补位）。

引擎归属校验（`test_hub_engine_attribution`）全都要「有人真的要过语音」才会动：
夜里引擎掉登记时没人说话 → 第二天第一批客户先听到别人的声音（lenient）或先收不到
语音（strict），我们才知道。hub 引擎目录是**合成前**就能读到的确定信号，所以这条
哨兵的全部价值就是**零流量也能响**。

与其他 watchdog 门禁同哲学：这类巡检的立命之本是**零误报**（一旦造噪就会被整条
关掉），故重点覆盖「不该告警」的路径 + 告警/重提/恢复时序。
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.ai.avatar_voice as av  # noqa: E402
import src.integrations.shared.event_bus as eb  # noqa: E402
from src.inbox.health_watchdog import HealthWatchdog  # noqa: E402

T0 = 1_000_000.0


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    def publish(self, etype, data):
        self.events.append((etype, data))


def _wd(monkeypatch, state, *, strict=False, engine="index_tts",
        enabled=True, hub_on=True, remind=None, wake=None, blockers=None,
        hub_fish=None):
    """按 __new__ 绕开 HealthWatchdog 重依赖（镜像 test_voice_outage 的脚手架）。

    ⚠ 三个 hub 出网口**一律替身**：``hub_engine_wake`` 是 POST（真跑会去动生产
    集群的引擎），``hub_vram_blockers`` 会打 GPU 采样端点。留一个没打桩，整个
    测试文件就变成了对生产 hub 的压测兼遥控器。
    """
    bus = _Bus()
    monkeypatch.setattr(eb, "get_event_bus", lambda: bus)
    monkeypatch.setattr(av, "hub_engine_directory_status",
                        lambda base_url, eng, **kw: {
                            "engine": str(eng), "state": state,
                            "available_engines": ["fish_speech"],
                            "offline_engines": ["index_tts"]})
    calls: list = []

    def _wake(base_url, eng, **kw):
        calls.append(str(eng))
        return wake if wake is not None else (True, "accepted")

    monkeypatch.setattr(av, "hub_engine_wake", _wake)
    monkeypatch.setattr(av, "hub_vram_blockers",
                        lambda base_url, **kw: {"hosts": blockers or []})

    class _CM:
        config = {
            "health_watchdog": {"hub_engine_remind": remind or {}},
            "avatar_voice": {
                "enabled": enabled,
                "voice_consistency": "strict" if strict else "lenient",
                "hub_fish": {"enabled": hub_on, "tts_engine": engine,
                             "base_url": "http://192.168.0.176:9000",
                             **(hub_fish or {})},
            },
        }

    wd = HealthWatchdog.__new__(HealthWatchdog)
    wd._app = types.SimpleNamespace()
    wd._config_manager = _CM()
    wd._hub_engine_down_since = 0.0
    wd._hub_engine_alerted = False
    wd._hub_engine_last_remind = 0.0
    wd.total_hub_engine_reminders = 0
    wd._wake_calls = calls          # 测试用：记录唤醒被调了几次、点名谁
    return wd, bus


def test_offline_alerts_only_after_grace(monkeypatch):
    """首见不叫（正常的泊车/唤醒循环几分钟就过去了），超 after_min 才轰人。"""
    wd, bus = _wd(monkeypatch, "offline")
    wd._check_hub_engine(now=T0)
    assert bus.events == []                      # 首见只记时刻
    wd._check_hub_engine(now=T0 + 300)           # 5min：宽限内
    assert bus.events == []
    wd._check_hub_engine(now=T0 + 25 * 60)       # 25min > 默认 20min
    assert [e[0] for e in bus.events] == ["hub_engine_alert"]
    payload = bus.events[0][1]
    assert payload["engine"] == "index_tts"
    assert payload["reminder"] is False
    assert payload["rate_key"] == "hub_engine:remind"
    assert wd.total_hub_engine_reminders == 1


def test_reminder_respects_interval(monkeypatch):
    wd, bus = _wd(monkeypatch, "offline")
    wd._check_hub_engine(now=T0)
    wd._check_hub_engine(now=T0 + 25 * 60)
    assert len(bus.events) == 1
    wd._check_hub_engine(now=T0 + 60 * 60)       # 1h < 默认 240min
    assert len(bus.events) == 1
    wd._check_hub_engine(now=T0 + 25 * 60 + 4 * 3600 + 60)
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is True


@pytest.mark.parametrize("state", ["unknown", "unlisted"])
def test_unknown_and_unlisted_stay_silent(monkeypatch, state):
    """目录拉不到＝hub 整体的活（另有告警）；查无此名＝配置笔误该由预检/首次合成报。
    让夜间告警去猜这两种只会造噪音，而噪音会让整条哨兵被关掉。"""
    wd, bus = _wd(monkeypatch, state)
    for i in range(6):
        wd._check_hub_engine(now=T0 + i * 3600)
    assert bus.events == []


def test_unknown_does_not_reset_an_existing_countdown(monkeypatch):
    """目录读数偶发拉不到，不能把已经攒了 20 分钟的离线计时清零
    （否则一台抖动的 hub 可以让告警永远差最后一分钟）。"""
    wd, bus = _wd(monkeypatch, "offline")
    wd._check_hub_engine(now=T0)
    monkeypatch.setattr(av, "hub_engine_directory_status",
                        lambda base_url, eng, **kw: {"engine": str(eng),
                                                     "state": "unknown"})
    wd._check_hub_engine(now=T0 + 10 * 60)
    assert wd._hub_engine_down_since == T0
    monkeypatch.setattr(av, "hub_engine_directory_status",
                        lambda base_url, eng, **kw: {"engine": str(eng),
                                                     "state": "offline"})
    wd._check_hub_engine(now=T0 + 25 * 60)
    assert len(bus.events) == 1


def test_recovery_only_after_having_alerted(monkeypatch):
    """没告过警的抖动恢复不发（防噪）；告过才补恢复通知并清零。"""
    wd, bus = _wd(monkeypatch, "offline")
    wd._check_hub_engine(now=T0)
    wd._check_hub_engine(now=T0 + 25 * 60)
    assert len(bus.events) == 1
    monkeypatch.setattr(av, "hub_engine_directory_status",
                        lambda base_url, eng, **kw: {"engine": str(eng),
                                                     "state": "ok"})
    wd._check_hub_engine(now=T0 + 30 * 60)
    assert bus.events[-1][1]["recovered"] is True
    assert wd._hub_engine_alerted is False
    assert wd._hub_engine_down_since == 0.0
    wd._check_hub_engine(now=T0 + 40 * 60)       # 再来一轮 ok 不重复发
    assert len(bus.events) == 2


def test_brief_park_never_alerts(monkeypatch):
    """泊车→唤醒 15 分钟内跑完＝正常运维行为，全程静默且不留状态。"""
    wd, bus = _wd(monkeypatch, "offline")
    wd._check_hub_engine(now=T0)
    wd._check_hub_engine(now=T0 + 10 * 60)
    monkeypatch.setattr(av, "hub_engine_directory_status",
                        lambda base_url, eng, **kw: {"engine": str(eng),
                                                     "state": "ok"})
    wd._check_hub_engine(now=T0 + 15 * 60)
    assert bus.events == []
    assert wd._hub_engine_down_since == 0.0


def test_silent_when_no_engine_pinned(monkeypatch):
    """没钉引擎＝接受 hub 自选，「被顶包」无从谈起。"""
    wd, bus = _wd(monkeypatch, "offline", engine="")
    wd._check_hub_engine(now=T0)
    wd._check_hub_engine(now=T0 + 2 * 3600)
    assert bus.events == []


@pytest.mark.parametrize("off", [{"enabled": False}, {"hub_on": False},
                                 {"remind": {"enabled": False}}])
def test_silent_when_subsystem_off(monkeypatch, off):
    wd, bus = _wd(monkeypatch, "offline", **off)
    wd._check_hub_engine(now=T0)
    wd._check_hub_engine(now=T0 + 2 * 3600)
    assert bus.events == []


def test_directory_probe_failure_is_swallowed(monkeypatch):
    """探测异常绝不能把整个 watchdog tick 炸掉（后面还有十几项巡检）。"""
    def _boom(*a, **kw):
        raise RuntimeError("boom")

    wd, bus = _wd(monkeypatch, "offline")
    monkeypatch.setattr(av, "hub_engine_directory_status", _boom)
    wd._check_hub_engine(now=T0)                 # 不抛
    assert bus.events == []


def test_every_engine_check_call_site_matches_the_signature():
    """所有 ``record_engine_check`` 调用点都裹在 ``except: pass`` 里——签名对不上
    ＝TypeError 被静默吞掉＝计数器永远 0，而看板/告警全靠这个数。

    2026-08-22 实锤：**合成前预检**（hub 目录说引擎离线，最该被看见的一档）多传了
    `profile=`，于是它从来没上报过一次。用真实签名逐点绑定参数，形参漂移即红。
    """
    import ast
    import inspect
    from pathlib import Path

    from src.ai.avatar_voice_stats import AvatarVoiceStats

    sig = inspect.signature(AvatarVoiceStats.record_engine_check)
    src_root = Path(__file__).parent.parent / "src"
    seen = 0
    for py in src_root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute)
                    and fn.attr == "record_engine_check"):
                continue
            seen += 1
            args = [None] * len(node.args)
            kwargs = {}
            for kw in node.keywords:
                assert kw.arg is not None, f"{py.name}: 禁止 **kwargs 展开调用"
                kwargs[kw.arg] = None
            # self 由绑定方法提供，这里补一个占位再逐点绑定
            sig.bind(None, *args, **kwargs)
    assert seen >= 2, "调用点扫描失效（重命名了？），门禁形同虚设"


def test_alert_text_separates_substitution_from_refusal():
    """两种后果对运维意味着完全不同的东西，不能糊成一句话：
    strict＝客户收不到语音（看得见的缺失）；lenient＝客户听到别人的声音（破绽）。"""
    from src.inbox.webhook_notifier import _build_message

    _, lenient = _build_message("hub_engine_alert", {
        "engine": "index_tts", "strict": False, "down_minutes": 35,
        "available_engines": ["fish_speech"]})
    assert "另一个人的声音" in lenient
    assert "fish_speech" in lenient              # 会被谁顶包，直接点名
    _, strict = _build_message("hub_engine_alert", {
        "engine": "index_tts", "strict": True, "down_minutes": 35})
    assert "拒发" in strict
    # 共同的排查纠偏：别去重启引擎进程（本次事故里它自己 /health 200）
    for txt in (lenient, strict):
        assert "/api/engine/start" in txt
        assert "model_loaded" in txt


# ── 先自救再轰人：唤醒 + 显存归因 ─────────────────────────────────────────────

def test_wake_is_attempted_once_per_alert_not_per_tick(monkeypatch):
    """唤醒挂在**告警节奏**上：宽限期内一次都不发（正常泊车循环不该被打扰），
    到点告警时发一次，重提周期内不再发——否则每 tick 一次 POST＝拿告警器当轮询器。"""
    wd, bus = _wd(monkeypatch, "offline")
    wd._check_hub_engine(now=T0)
    wd._check_hub_engine(now=T0 + 10 * 60)
    assert wd._wake_calls == []                  # 宽限期内不动手
    wd._check_hub_engine(now=T0 + 25 * 60)
    assert wd._wake_calls == ["index_tts"]       # 只点名自己钉的那个
    wd._check_hub_engine(now=T0 + 60 * 60)       # 重提周期内
    assert len(wd._wake_calls) == 1
    wd._check_hub_engine(now=T0 + 25 * 60 + 4 * 3600 + 60)
    assert len(wd._wake_calls) == 2


def test_wake_outcome_reaches_the_operator(monkeypatch):
    """受理 vs 失败，运维要做的事完全不同（一个是等，一个是起床），必须分开说。"""
    from src.inbox.webhook_notifier import _build_message

    wd, bus = _wd(monkeypatch, "offline", wake=(True, "starting"))
    wd._check_hub_engine(now=T0)
    wd._check_hub_engine(now=T0 + 25 * 60)
    assert bus.events[0][1]["wake"] == "accepted:starting"
    _, txt = _build_message("hub_engine_alert", bus.events[0][1])
    assert "已受理" in txt and "先别动手" in txt

    wd2, bus2 = _wd(monkeypatch, "offline", wake=(False, "insufficient_vram"))
    wd2._check_hub_engine(now=T0)
    wd2._check_hub_engine(now=T0 + 25 * 60)
    assert bus2.events[0][1]["wake"] == "failed:insufficient_vram"
    _, txt2 = _build_message("hub_engine_alert", bus2.events[0][1])
    assert "需要人工介入" in txt2 and "insufficient_vram" in txt2


def test_wake_can_be_disabled(monkeypatch):
    """共享 hub 上运营可能是**故意**把引擎泊掉的，得留一个不跟人抢的开关。
    开关与合成路径同键（``hub_fish.auto_wake``）——两处各一个键＝关了一个以为
    关全了，而另一条还在替他动生产引擎。"""
    wd, bus = _wd(monkeypatch, "offline", hub_fish={"auto_wake": False})
    wd._check_hub_engine(now=T0)
    wd._check_hub_engine(now=T0 + 25 * 60)
    assert wd._wake_calls == []
    assert len(bus.events) == 1                  # 告警照发，只是不自己动手
    assert bus.events[0][1]["wake"] == ""


def test_wake_and_vram_failures_never_block_the_alert(monkeypatch):
    """补充信息是锦上添花：显存端点/唤醒端点挂了也**必须**把告警发出去——
    否则 hub 越不健康，我们越发不出告警（正好反了）。"""
    def _boom(*a, **kw):
        raise RuntimeError("hub down")

    wd, bus = _wd(monkeypatch, "offline")
    monkeypatch.setattr(av, "hub_engine_wake", _boom)
    monkeypatch.setattr(av, "hub_vram_blockers", _boom)
    wd._check_hub_engine(now=T0)
    wd._check_hub_engine(now=T0 + 25 * 60)
    assert len(bus.events) == 1
    assert bus.events[0][1]["wake"] == "failed:exception"
    assert bus.events[0][1]["vram_hosts"] == []


def test_alert_names_who_is_holding_the_vram(monkeypatch):
    """「引擎离线」几乎总是「显存被谁占着」——不指名道姓，运维还得自己翻 GPU 面板。
    2026-08-22 实测：唱歌工作室两个服务占 16.7G，质量轨 index_tts 需 ~8.7G 起不来。"""
    from src.inbox.webhook_notifier import _build_message

    wd, bus = _wd(monkeypatch, "offline", blockers=[{
        "ip": "192.168.0.176", "free_mb": 5918, "pressure": "med",
        "parkable": [
            {"name": "ace_studio", "label": "原创歌工作室 (ACE-Step 文本成曲)",
             "mem_mb": 10915, "busy": False},
            {"name": "singing", "label": "唱歌工作室 (AI 翻唱·YingMusic-SVC)",
             "mem_mb": 5763, "busy": False},
        ]}])
    wd._check_hub_engine(now=T0)
    wd._check_hub_engine(now=T0 + 25 * 60)
    _, txt = _build_message("hub_engine_alert", bus.events[0][1])
    assert "原创歌工作室" in txt and "10.7G" in txt
    assert "192.168.0.176" in txt and "5.8G" in txt


def test_vram_blockers_only_lists_services_that_can_yield(monkeypatch):
    """只列 online 且 parkable 的：core 服务（如 fish_tts）被列出来只会诱导运维
    去停关键链路；已 parked 的列出来是噪音（它已经让过路了）。"""
    payload = {
        "local": {"ip": "10.0.0.1", "mem_used_mb": 30000, "mem_total_mb": 32000,
                  "pressure": "high", "services": [
                      {"name": "core_tts", "online": True, "parkable": False,
                       "core": True, "vram_seen_mb": 4534},
                      {"name": "parked_one", "online": False, "parkable": True,
                       "parked": True, "vram_gb": 3.5},
                      {"name": "hog", "online": True, "parkable": True,
                       "vram_seen_mb": 10915},
                  ]},
        "remotes": [{"ip": "10.0.0.2", "mem_used_mb": 1, "mem_total_mb": 2,
                     "services": [{"name": "nothing_parkable", "online": True,
                                   "parkable": False, "vram_gb": 1}]}],
    }
    monkeypatch.setattr(av, "_fetch_gpu_overview", lambda url, t: payload)
    av._gpu_overview_cache.clear()
    out = av.hub_vram_blockers("http://hub")
    assert [h["ip"] for h in out["hosts"]] == ["10.0.0.1"]   # 无可让路者的主机不占版面
    host = out["hosts"][0]
    assert [s["name"] for s in host["parkable"]] == ["hog"]
    assert host["free_mb"] == 2000


def test_vram_blockers_report_current_usage_not_historic_peak(monkeypatch):
    """实测当前占用优先于历史峰值（2026-08-22 生产实弹，首版写反了）。

    真实数据：176 上 ``singing`` 报 ``mem_mb=0``（在线但没驻留——hub 的 idle_park
    已经把权重卸了，进程还挂着）而 ``vram_seen_mb=5763``（它**曾经**吃到 5.7G）。
    按峰值优先，告警会说「泊掉唱歌工作室可让出 5.7G」——**实际让出 0**。运维照单
    去泊、空闲显存一点没动，这一行从此没人信；更糟的是它把账算到唱歌线头上，而
    真正占卡的是孤儿进程 + 常驻 ollama。所以「实测 0」必须直接不进候选名单：
    自信地点错人比不点名更贵。

    同时钉住 remote 的降级档：那些节点 hub 没现场采样过，只有标称 ``vram_gb``，
    必须标 ``est`` 让文案说「约」——不能把估算当实测报，否则运维会按标称算「泊掉
    这俩就够」然后发现不够。
    """
    payload = {
        "local": {"ip": "10.0.0.1", "mem_used_mb": 30932, "mem_total_mb": 32607,
                  "pressure": "high", "services": [
                      # 真在占卡：实测 8545（峰值 8685 略高，不该用峰值）
                      {"name": "index_tts", "online": True, "parkable": True,
                       "mem_mb": 8545, "vram_seen_mb": 8685},
                      # 生产原样：在线、峰值很大、但**当前 0**
                      {"name": "singing", "online": True, "parkable": True,
                       "mem_mb": 0, "vram_seen_mb": 5763},
                  ]},
        "remotes": [{"ip": "10.0.0.2", "mem_used_mb": 3418, "mem_total_mb": 12288,
                     "services": [{"name": "qwen3_tts", "online": True,
                                   "parkable": True, "vram_gb": 3.2}]}],
    }
    monkeypatch.setattr(av, "_fetch_gpu_overview", lambda url, t: payload)
    av._gpu_overview_cache.clear()
    hosts = {h["ip"]: h for h in av.hub_vram_blockers("http://hub")["hosts"]}

    local = hosts["10.0.0.1"]["parkable"]
    assert [s["name"] for s in local] == ["index_tts"], "实测 0 的服务不得进候选"
    assert local[0]["mem_mb"] == 8545, "要实测值 8545，不是历史峰值 8685"
    assert local[0]["est"] is False

    remote = hosts["10.0.0.2"]["parkable"][0]
    assert remote["mem_mb"] == 3276 and remote["est"] is True   # 标称 3.2G → 标估算

    # 文案层：实测不带限定词、估算带「约」
    from src.inbox.webhook_notifier import _vram_lines
    txt = _vram_lines(list(hosts.values()), top=2)
    assert "8.3G" in txt and "约8.3G" not in txt
    assert "约3.2G" in txt
    assert "唱歌" not in txt and "singing" not in txt


def test_vram_blockers_keep_unsampled_zero_as_a_candidate(monkeypatch):
    """估算档的 0 **不能**当「让不出东西」裁掉——那个 0 可能只是没报字段。

    实测档的 0 是确定读数（hub 现场采样得来），估算档缺字段与真的空占用无法区分，
    宁可列出来让人看一眼。反过来写会让 remote 主机上真正的占用方整台消失。
    """
    payload = {"local": {"ip": "10.0.0.9", "mem_used_mb": 11000,
                         "mem_total_mb": 12000, "pressure": "high",
                         "services": [{"name": "mystery", "online": True,
                                       "parkable": True}],
                         "extras": [{"pid": 7, "mem_mb": 4096}]}}
    monkeypatch.setattr(av, "_fetch_gpu_overview", lambda url, t: payload)
    av._gpu_overview_cache.clear()
    host = av.hub_vram_blockers("http://hub")["hosts"][0]
    assert [s["name"] for s in host["parkable"]] == ["mystery"]
    assert host["parkable"][0]["est"] is True


def test_vram_blockers_close_the_books_and_never_blame_the_victim(monkeypatch):
    """账要对得上、且绝不建议「泊掉正要救的那个引擎」。

    两条都是 2026-08-22 实弹打出来的（生产 176 现场读数照抄进夹具）：

    ① **自指建议**：修掉「历史峰值冒充当前占用」后，176 的可让路候选只剩
       ``index_tts`` 自己 8.5G——而它就是被饿死的质量轨引擎。此前这条被 singing
       的 5.7G 幻影盖着，幻影一除就露出来。排掉它之后该主机一个候选都不剩，
       那才是正确结论。
    ② **常驻大模型整块从账上消失**：ollama keep_alive 驻留的模型既不在
       ``services`` 也不在 ``extras``。少了这一笔，告警上写「可让路：无；另有
       8.6G 无主」而机器已用 30.9/32.6G——运维只会得出「读数坏了」。而它恰恰是
       唯一还能自动腾出来的一笔（hub 自己会驱逐 LAN 大模型让路），必须与「无主
       进程（只能上机）」分开报，因为处置手段完全相反。
    """
    payload = {"local": {
        "ip": "192.168.0.176", "mem_used_mb": 30940, "mem_total_mb": 32607,
        "pressure": "high",
        "services": [
            {"name": "index_tts", "online": True, "parkable": True,
             "mem_mb": 8545, "vram_seen_mb": 8685},
            {"name": "singing", "online": True, "parkable": True,
             "mem_mb": 0, "vram_seen_mb": 5763},
        ],
        "extras": [{"pid": 24988, "label": "python.exe", "mem_mb": 5192,
                    "service": None, "off_roster": True},
                   {"pid": 25564, "label": "python.exe", "mem_mb": 3573,
                    "service": None, "off_roster": True},
                   {"pid": 0, "label": "desktop", "mem_mb": 648,
                    "service": "desktop"}],
        "ollama": {"models": [{"name": "hy-mt2-7b-official", "vram_gb": 4.7},
                              {"name": "qwen3-vl:8b", "vram_gb": 5.1}]},
    }}
    monkeypatch.setattr(av, "_fetch_gpu_overview", lambda url, t: payload)
    av._gpu_overview_cache.clear()

    host = av.hub_vram_blockers("http://hub", exclude="index_tts")["hosts"][0]
    assert [s["name"] for s in host["parkable"]] == ["singing"], (
        "要救的引擎不得列为让路候选；singing 实测 0 但同台有无主显存 ⇒ 疑似候选")
    assert host["parkable"][0]["unattributed"] is True
    assert host["parkable"][0]["mem_mb"] == 5763, "额度取 峰值 与 无主池 的较小者"
    assert host["llm_mb"] == 10034, "常驻大模型必须单列（4.7+5.1G）"
    assert host["extras_mb"] == 8765, "无主进程仍按原口径，桌面合计不计入"

    from src.inbox.webhook_notifier import _vram_lines
    txt = _vram_lines([host], top=3)
    assert "疑似5.6G" in txt, "证据只到「疑似」，不许当实测承诺"
    assert "9.8G" in txt and "自动驱逐" in txt, "大模型那笔要说明无需上机"
    assert "8.6G" in txt and "先试泊车" in txt, (
        "疑似项与无主池重叠，不点出来运维会把两笔相加规划出假余量")
    assert "index_tts" not in txt
    assert "YingMusic 疑似" not in txt, "标签不得截在括号中间（告警看着像坏了）"


def test_measured_zero_is_only_dropped_when_the_books_balance(monkeypatch):
    """实测 0 ⇒ 剔除，**前提是同台没有无主显存**。

    这是 ``unattributed`` 那条通道的反面：hub 把每一块显存都归到了服务上，此时
    「``online`` 且 ``mem_mb=0``」是确定读数（idle_park 卸了权重、进程空挂），
    泊掉它让出的确实是 0，列出来是冤枉。少了这条反向门禁，上一个测试可以被
    「无脑放行所有实测 0」蒙过去，等于把首版那个「峰值冒充现况」的 bug 放回来。
    """
    payload = {"local": {"ip": "10.0.0.5", "mem_used_mb": 9000,
                         "mem_total_mb": 12000, "pressure": "high",
                         "services": [{"name": "idle_one", "online": True,
                                       "parkable": True, "mem_mb": 0,
                                       "vram_seen_mb": 5763}],
                         "extras": [{"pid": 0, "label": "desktop",
                                     "mem_mb": 700, "service": "desktop"}]}}
    monkeypatch.setattr(av, "_fetch_gpu_overview", lambda url, t: payload)
    av._gpu_overview_cache.clear()
    assert av.hub_vram_blockers("http://hub")["hosts"] == [], (
        "既无可让路项也无无主/大模型显存的主机不该占告警版面")


def test_vram_blockers_fail_open_on_unreachable_hub(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("timeout")

    monkeypatch.setattr(av, "_fetch_gpu_overview", _boom)
    av._gpu_overview_cache.clear()
    assert av.hub_vram_blockers("http://hub") == {"hosts": []}


def test_demand_driven_wake_is_throttled_and_never_blocks(monkeypatch):
    """合成路径撞见离线时的唤醒（把降级窗口从「看门狗宽限 20min」压到一次冷载）。

    三条硬约束，缺一条就从修复变成事故源：① 爆发来消息时不能变 POST 洪水（节流）；
    ② 不阻塞本条（后台线程，本条照旧 strict 拒发 / lenient 顶包）；③ 唤醒炸了
    绝不外溢（本条的成败与它无关）。
    """
    import threading as _th

    started: list = []
    monkeypatch.setattr(av, "hub_engine_wake",
                        lambda base, eng, **kw: (True, "starting"))
    av._wake_last.clear()

    real_thread = _th.Thread

    def _spy(*a, **kw):
        started.append(kw.get("name"))
        t = real_thread(*a, **kw)
        return t

    monkeypatch.setattr(av.threading, "Thread", _spy)
    assert av.hub_engine_wake_bg("http://hub", "index_tts", now=T0) is True
    # 爆发窗内后续请求一律被节流（冷载本就 ~18s，节流窗盖住它）
    for i in range(5):
        assert av.hub_engine_wake_bg("http://hub", "index_tts",
                                     now=T0 + i) is False
    assert len(started) == 1
    # 别的引擎/别的 hub 各自独立计时（一个引擎的节流不该锁死另一个）
    assert av.hub_engine_wake_bg("http://hub", "fish_speech", now=T0) is True
    assert av.hub_engine_wake_bg("http://other", "index_tts", now=T0) is True
    # 节流窗过后可再试
    assert av.hub_engine_wake_bg("http://hub", "index_tts", now=T0 + 91) is True
    # 展示名/服务名归一到同一个节流键，别绕开节流
    assert av.hub_engine_wake_bg("http://hub", "IndexTTS-2", now=T0 + 92) is False


def test_demand_driven_wake_swallows_everything(monkeypatch):
    """无目标 → 不派发；线程创建/唤醒本身炸了 → 静默 False，绝不打断合成路径。"""
    av._wake_last.clear()
    assert av.hub_engine_wake_bg("", "index_tts", now=T0) is False
    assert av.hub_engine_wake_bg("http://hub", "", now=T0) is False

    def _boom(*a, **kw):
        raise RuntimeError("no threads")

    monkeypatch.setattr(av.threading, "Thread", _boom)
    assert av.hub_engine_wake_bg("http://hub", "index_tts", now=T0) is False


def test_synthesis_path_requests_wake_on_offline_engine():
    """静态接线钉住：预检拦下时必须捎一句唤醒，且开关可关。

    只钉「调用点在、且被 auto_wake 守着」——真实行为由上面两条纯函数门禁覆盖，
    这里防的是「有人重构预检时把唤醒顺手删了」（删了不会红任何断言，只是降级窗口
    悄悄回到 20 分钟）。
    """
    src = (Path(__file__).parent.parent / "src" / "ai"
           / "tts_pipeline.py").read_text(encoding="utf-8")
    idx = src.index('rv.extra["hub_engine_offline"] = hub_engine')
    seg = src[idx:idx + 1200]
    assert "hub_engine_wake_bg" in seg, "预检拦下时没请求唤醒"
    assert 'hf.get("auto_wake", True)' in seg, "唤醒必须可关（共享 hub 上别跟人抢）"


def test_wake_read_timeout_counts_as_dispatched_not_failure(monkeypatch):
    """2026-08-22 实弹：5s 超时下 POST 抛 TimeoutError，而引擎 **35s 后真的上岗了**
    （176 空闲显存 5.9G→1.0G＝模型确实载入）。按「失败」上报＝把人从床上叫起来修
    一个已经修好的问题，一两次之后这个字段就没人信了。

    异常分两类对应两种事实：裸 TimeoutError 是 http.client 等响应时抛的（连上了、
    请求已送达）＝已派发；URLError 族连都没连上＝真失败。
    """
    monkeypatch.setattr(av, "_fetch_engine_directory", lambda url, t: {
        "index_tts": av.EngineEntry(False, 22050, "index_tts")})
    av._engine_dir_cache.clear()

    def _timeout(req, timeout=0):
        raise TimeoutError("timed out")

    monkeypatch.setattr(av.urllib.request, "urlopen", _timeout)
    assert av.hub_engine_wake("http://hub", "index_tts") == (True, "dispatched")

    def _unreachable(req, timeout=0):
        raise av.urllib.error.URLError("no route")

    monkeypatch.setattr(av.urllib.request, "urlopen", _unreachable)
    ok, detail = av.hub_engine_wake("http://hub", "index_tts")
    assert ok is False and detail == "URLError"


def test_dispatched_wake_reads_as_dont_get_up(monkeypatch):
    """「已派发」与「已受理」对运维是同一个指示：等下一轮恢复通知，别动手。"""
    from src.inbox.webhook_notifier import _build_message

    wd, bus = _wd(monkeypatch, "offline", wake=(True, "dispatched"))
    wd._check_hub_engine(now=T0)
    wd._check_hub_engine(now=T0 + 25 * 60)
    _, txt = _build_message("hub_engine_alert", bus.events[0][1])
    assert "先别动手" in txt and "需要人工介入" not in txt


def test_wake_targets_the_service_namespace_not_the_display_name(monkeypatch):
    """hub 的唤醒/泊车端点吃的是**服务名**（目录 backend 字段）：展示名
    ``index_tts2`` 的服务名是 ``index_tts``。拿展示名去调会静默无效——
    这正是本模块要替调用方记住的那件事（2026-08-22 实测两名并存）。"""
    posted: list = []

    monkeypatch.setattr(av, "_fetch_engine_directory", lambda url, t: {
        "index_tts2": av.EngineEntry(False, 22050, "index_tts"),
        "index_tts": av.EngineEntry(False, 22050, "index_tts"),
    })
    av._engine_dir_cache.clear()

    class _Resp:
        def read(self):
            return b'{"ok": true, "state": "starting"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(req, timeout=0):
        posted.append(req.full_url)
        return _Resp()

    monkeypatch.setattr(av.urllib.request, "urlopen", _urlopen)
    ok, detail = av.hub_engine_wake("http://hub", "index_tts2")
    assert ok is True and detail == "starting"
    assert "name=index_tts&" in posted[0] or posted[0].endswith("name=index_tts")
    assert "index_tts2" not in posted[0]

# ── 无主显存 & 「在岗但答不上来」 ────────────────────────────────────────────────
# 2026-08-22 实弹的两个盲区：① 176 上 10G 是**无主 python.exe**，parkable 只列得出
# singing 的 5.7G——运维泊完还是不够、线索就断了；② index_tts 目录 available:true、
# 进程 /health 200，但同卡显存 97% 满 + util 100%，短句实测 36~69s vs 生产预算 30s
# ⇒ 每发必超时回落，客户照样听到别人的声音。绿灯**如实地说谎**是最坏的一种坏。


def _ov(extras=None, services=None, ip="10.0.0.1", used=30000, total=32000):
    return {"local": {"ip": ip, "mem_used_mb": used, "mem_total_mb": total,
                      "pressure": "high",
                      "services": services if services is not None else [
                          {"name": "index_tts", "online": True, "parkable": True,
                           "vram_seen_mb": 8685}],
                      "extras": extras or []},
            "remotes": []}


def test_orphan_vram_is_attributed_not_silently_dropped(monkeypatch):
    """无主进程占的显存必须报出来：它不可泊车，却常是最大一块。"""
    payload = _ov(extras=[
        {"pid": 1, "label": "python.exe", "mem_mb": 6490, "service": None},
        {"pid": 2, "label": "python.exe", "mem_mb": 3573, "service": None},
        # 桌面/系统合计是常驻开销，不是「谁忘了关的脚本」，不该算进去
        {"pid": 0, "label": "desktop", "mem_mb": 632, "service": "desktop"},
    ])
    monkeypatch.setattr(av, "_fetch_gpu_overview", lambda url, t: payload)
    av._gpu_overview_cache.clear()
    host = av.hub_vram_blockers("http://hub")["hosts"][0]
    assert host["extras_mb"] == 10063


def test_host_with_only_orphan_vram_still_reported(monkeypatch):
    """没有任何可泊项、只有无主显存的主机**不能**被整台滤掉——它正是根因所在。
    （旧口径只看 parkable，这台会消失，于是告警里根本没有那 10G 的踪影。）"""
    payload = _ov(services=[], extras=[{"pid": 1, "mem_mb": 9000, "service": None}])
    monkeypatch.setattr(av, "_fetch_gpu_overview", lambda url, t: payload)
    av._gpu_overview_cache.clear()
    hosts = av.hub_vram_blockers("http://hub")["hosts"]
    assert [h["ip"] for h in hosts] == ["10.0.0.1"]
    assert hosts[0]["parkable"] == [] and hosts[0]["extras_mb"] == 9000


def test_orphan_vram_reaches_the_operator(monkeypatch):
    """告警文案要点名「泊车腾不出来，得上机排查」，否则运维会一直泊错东西。"""
    from src.inbox.webhook_notifier import _build_message
    _, text = _build_message(
        "hub_engine_alert",
        {"engine": "index_tts", "strict": True, "url": "http://hub",
         "vram_hosts": [{"ip": "10.0.0.1", "free_mb": 913, "pressure": "high",
                         "parkable": [{"name": "singing", "label": "唱歌工作室",
                                       "mem_mb": 5763}],
                         "extras_mb": 10063}]})
    assert "唱歌工作室" in text          # 可让路的仍要点名
    assert "9.8G" in text                # 10063MB 的无主显存
    assert "无主进程" in text


def test_orphan_line_omitted_when_negligible(monkeypatch):
    """不到 1G 的零碎不值一行——告警每多一行没用的，下次就少一个人读完。"""
    from src.inbox.webhook_notifier import _build_message
    _, text = _build_message(
        "hub_engine_alert",
        {"engine": "index_tts", "url": "http://hub",
         "vram_hosts": [{"ip": "10.0.0.1", "free_mb": 913,
                         "parkable": [{"name": "singing", "label": "唱歌",
                                       "mem_mb": 5763}], "extras_mb": 300}]})
    assert "无主进程" not in text


def test_on_duty_light_admits_host_pressure(monkeypatch):
    """`state=ok` 旁边必须带宿主压力：只亮绿灯会**如实地说谎**（读数全对、结论全错）。"""
    monkeypatch.setattr(av, "_fetch_engine_directory", lambda url, t: {
        "index_tts": av.EngineEntry(True, 22050, "index_tts")})
    monkeypatch.setattr(av, "_fetch_gpu_overview", lambda url, t: _ov(
        ip="192.168.0.176"))
    av._engine_dir_cache.clear()
    av._gpu_overview_cache.clear()
    av._overview_warming.clear()
    st = av.hub_engine_directory_status("http://hub", "index_tts")
    assert st["state"] == "ok"
    # 首次缓存全冷 → 后台预热、本次不等（看板端点不许卡 8 秒）
    assert st["host"] == "" and st["pressure"] == ""
    for _ in range(50):                       # 等预热线程落缓存
        if av._gpu_overview_cache:
            break
        time.sleep(0.05)
    st = av.hub_engine_directory_status("http://hub", "index_tts")
    assert st["host"] == "192.168.0.176"
    assert st["pressure"] == "high" and st["host_free_mb"] == 2000


def test_pressure_hint_prefers_stale_cache_over_blocking(monkeypatch):
    """陈旧读数照用（显存压力是慢变量），但**age 要如实说**。
    绝不为一个提示行让看板同步等 1.5~4s 的现场采样。"""
    calls: list = []

    def _fetch(url, t):
        calls.append(t)
        return _ov(ip="192.168.0.176")

    monkeypatch.setattr(av, "_fetch_engine_directory", lambda url, t: {
        "index_tts": av.EngineEntry(True, 22050, "index_tts")})
    monkeypatch.setattr(av, "_fetch_gpu_overview", _fetch)
    av._engine_dir_cache.clear()
    # 9 分钟前的快照：早过 TTL(30s)，但在容忍窗(10min)内 → 照用、零网络
    av._gpu_overview_cache.clear()
    av._gpu_overview_cache["http://hub"] = (time.time() - 540.0,
                                            _ov(ip="192.168.0.176"))
    st = av.hub_engine_directory_status("http://hub", "index_tts")
    assert st["pressure"] == "high"
    assert st["pressure_age_s"] >= 539       # 老，但如实报老
    assert calls == []                       # 没为它付一次同步采样


def test_pressure_hint_never_breaks_the_status(monkeypatch):
    """概览拉不到 / 引擎不在任何主机的服务表里 → 空壳，绝不让整张状态崩掉。"""
    monkeypatch.setattr(av, "_fetch_engine_directory", lambda url, t: {
        "index_tts": av.EngineEntry(True, 22050, "index_tts")})
    monkeypatch.setattr(av, "_fetch_gpu_overview",
                        lambda url, t: (_ for _ in ()).throw(OSError("boom")))
    av._engine_dir_cache.clear()
    av._gpu_overview_cache.clear()
    av._overview_warming.clear()
    st = av.hub_engine_directory_status("http://hub", "index_tts")
    assert st["state"] == "ok" and st["host"] == ""

    # 概览能拉到但引擎不在任何服务表里（跨机漂移/改名）→ 同样是空壳而非猜
    monkeypatch.setattr(av, "_fetch_gpu_overview", lambda url, t: _ov(
        services=[{"name": "someone_else", "online": True, "parkable": True,
                   "vram_seen_mb": 1}]))
    av._gpu_overview_cache.clear()
    av._overview_warming.clear()
    av.hub_engine_directory_status("http://hub", "index_tts")
    for _ in range(50):
        if av._gpu_overview_cache:
            break
        time.sleep(0.05)
    st = av.hub_engine_directory_status("http://hub", "index_tts")
    assert st["state"] == "ok" and st["host"] == ""


def test_overview_warm_does_not_stack_threads(monkeypatch):
    """看板在轮询：同一 base 只许一个预热在飞，否则每轮都叠一个线程。"""
    import threading as _th

    gate = _th.Event()
    started: list = []

    def _slow(url, t):
        started.append(url)
        gate.wait(5)
        return _ov()

    monkeypatch.setattr(av, "_fetch_gpu_overview", _slow)
    av._gpu_overview_cache.clear()
    av._overview_warming.clear()
    for _ in range(5):
        av._warm_gpu_overview_bg("http://hub")
    time.sleep(0.2)
    assert len(started) == 1
    gate.set()

# ── 超时熔断（2026-08-22「在岗但答不上来」）─────────────────────────────────
# 目录 available:true + 引擎 /health 200，但同卡显存 97% 满 → 短句 36~69s vs 预算
# 30s ⇒ 每一发都超时。前两道闸都不响（预检看 available、指纹闸要先拿到音频），于是
# 每条消息白等 30 秒。这批门禁守的是：熔断只认实测超时、开路期真的省掉那 30 秒、
# 半开只放一发、任何成功立即清零——以及「绝不在引擎还答得上来时误杀语音」。


@pytest.fixture(autouse=True)
def _clean_breaker():
    # GPU 概览缓存一起清：它现在参与熔断决策（宿主吃紧→首振即开路），留一份
    # "high" 在进程里会让后面那些「必须维持两振」的门禁莫名转红。
    av._breaker.clear()
    av._gpu_overview_cache.clear()
    yield
    av._breaker.clear()
    av._gpu_overview_cache.clear()


def test_breaker_needs_two_timeouts_not_one():
    """一次超时可能只是长句/偶发抖动。误开路＝白丢五分钟语音，宁可多等一轮。"""
    av.note_hub_synth_outcome("http://hub", "index_tts", timed_out=True, now=T0)
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=T0) is False
    assert av.hub_synth_breaker_state(
        "http://hub", "index_tts", now=T0)["breaker"] == "tripping"

    av.note_hub_synth_outcome("http://hub", "index_tts", timed_out=True, now=T0 + 5)
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=T0 + 5) is True


def _seed_pressure(monkeypatch, level, *, service="index_tts"):
    """把 GPU 概览缓存做热（不发网络），让宿主压力可读。"""
    av._gpu_overview_cache["http://hub"] = (time.time(), {
        "local": {"ip": "192.168.0.176", "pressure": level,
                  "mem_used_mb": 23000, "mem_total_mb": 24000,
                  "services": [{"name": service}]},
        "remotes": [],
    })
    monkeypatch.setattr(av, "_gpu_overview_cache", av._gpu_overview_cache,
                        raising=False)


def test_confirmed_starvation_trips_on_the_first_timeout(monkeypatch):
    """宿主已实测吃紧时，第二振没有任何不确定性——等它只是多赔一个客户。

    两振规则的存在理由是「一次超时可能只是长句/偶发抖动」。但当同一台宿主的显存
    压力**已经被量到** high 时，超时的原因已经定案（事故当天：同卡 97% 满 → 8 字
    要 36~69s → 预算 30s 必超时），此时坚持等第二发＝让第二个客户也白等满一轮预算。
    """
    _seed_pressure(monkeypatch, "high")
    av.note_hub_synth_outcome("http://hub", "index_tts", timed_out=True, now=T0)
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=T0) is True
    assert av.hub_synth_breaker_state(
        "http://hub", "index_tts", now=T0)["breaker"] == "open"


def test_healthy_host_still_gets_the_benefit_of_the_doubt(monkeypatch):
    """压力不高＝超时原因未定案 → 必须维持两振，绝不因为「快一点」误杀语音。"""
    _seed_pressure(monkeypatch, "med")
    av.note_hub_synth_outcome("http://hub", "index_tts", timed_out=True, now=T0)
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=T0) is False
    assert av.hub_synth_breaker_state(
        "http://hub", "index_tts", now=T0)["breaker"] == "tripping"


def test_fast_trip_never_blocks_or_touches_the_network(monkeypatch):
    """判定挂在一条已经等了 30s 的失败路径上：不许再发任何 HTTP。

    缓存全冷时**连后台预热都不许触发**——顺手起一次 fetch 看着无害，但它发生在
    「显存已经打满」的时刻，那正是每一跳都容易挂住的时刻。
    """
    av._gpu_overview_cache.pop("http://hub", None)

    def _boom(*a, **k):
        raise AssertionError("失败路径上不得发起 GPU 概览请求")

    monkeypatch.setattr(av, "_fetch_gpu_overview", _boom, raising=False)
    monkeypatch.setattr(av, "_warm_gpu_overview_bg", _boom, raising=False)
    av.note_hub_synth_outcome("http://hub", "index_tts", timed_out=True, now=T0)
    # 冷缓存 ⇒ 退回两振规则（少一次加速，不是少一层保护）
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=T0) is False


def test_fast_trip_judgement_cannot_deadlock_or_break_the_breaker(monkeypatch):
    """判定要拿同一把 `_engine_dir_lock`（普通 Lock 非 RLock）→ 必须在临界区外算完。

    这条门禁存在的原因很具体：把 `_starvation_confirmed` 顺手写进 `with
    _engine_dir_lock:` 里就是死锁，而死锁在这里的表现是**语音链整条卡住**，
    比熔断晚开一振严重得多。同时钉住「判定自己抛异常也只退回两振」。
    """
    _seed_pressure(monkeypatch, "high")
    av.note_hub_synth_outcome("http://hub", "index_tts", timed_out=True, now=T0)
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=T0) is True

    av._breaker.clear()

    def _boom(*a, **k):
        raise RuntimeError("pressure lookup exploded")

    monkeypatch.setattr(av, "_engine_host_pressure", _boom, raising=False)
    av.note_hub_synth_outcome("http://hub", "index_tts", timed_out=True, now=T0)
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=T0) is False


def test_any_success_clears_the_streak():
    """引擎恢复了就该立刻用起来——不该因为「刚才超时过」继续跳过。"""
    av.note_hub_synth_outcome("http://hub", "index_tts", timed_out=True, now=T0)
    av.note_hub_synth_outcome("http://hub", "index_tts", timed_out=False, now=T0 + 1)
    av.note_hub_synth_outcome("http://hub", "index_tts", timed_out=True, now=T0 + 2)
    # 中间那次成功清零 ⇒ 现在只是「第 1 次」，不该开路
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=T0 + 2) is False


def test_half_open_lets_exactly_one_probe_through():
    """并发消息不该在窗口一到时集体去撞恢复中的引擎（那是 N×30s 的白等）。"""
    for i in range(2):
        av.note_hub_synth_outcome(
            "http://hub", "index_tts", timed_out=True, now=T0 + i)
    later = T0 + av._BREAKER_OPEN_SEC + 10
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=later) is False
    # 第二、第三个到达者仍被挡（探路名额已被拿走）
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=later) is True
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=later) is True
    assert av.hub_synth_breaker_state(
        "http://hub", "index_tts", now=later)["breaker"] == "half_open"

    # 探路成功 → 闭合，所有人放行
    av.note_hub_synth_outcome(
        "http://hub", "index_tts", timed_out=False, now=later + 20)
    assert av.hub_synth_breaker_open(
        "http://hub", "index_tts", now=later + 21) is False
    assert av.hub_synth_breaker_state(
        "http://hub", "index_tts", now=later + 21)["breaker"] == "closed"


def test_failed_probe_reopens_the_window():
    """探路又超时 ⇒ 重新开路整窗，而不是每轮都放一发继续白等。"""
    for i in range(2):
        av.note_hub_synth_outcome(
            "http://hub", "index_tts", timed_out=True, now=T0 + i)
    later = T0 + av._BREAKER_OPEN_SEC + 10
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=later) is False
    av.note_hub_synth_outcome(
        "http://hub", "index_tts", timed_out=True, now=later + 30)
    assert av.hub_synth_breaker_open(
        "http://hub", "index_tts", now=later + 31) is True
    st = av.hub_synth_breaker_state("http://hub", "index_tts", now=later + 31)
    assert st["breaker"] == "open" and st["reopen_in_s"] > 200


def test_breaker_is_per_engine_and_per_hub():
    """一个引擎被挤爆不该连坐另一个（177 上的 fish 可能好得很）。"""
    for i in range(2):
        av.note_hub_synth_outcome(
            "http://hub", "index_tts", timed_out=True, now=T0 + i)
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=T0 + 2) is True
    assert av.hub_synth_breaker_open("http://hub", "fish_speech", now=T0 + 2) is False
    assert av.hub_synth_breaker_open("http://other", "index_tts", now=T0 + 2) is False


def test_engine_aliases_share_one_breaker():
    """目录叫 index_tts2、配置写 index_tts——两个名字是同一台机器上的同一个引擎。"""
    av.note_hub_synth_outcome("http://hub", "index_tts2", timed_out=True, now=T0)
    av.note_hub_synth_outcome("http://hub", "IndexTTS-2", timed_out=True, now=T0 + 1)
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=T0 + 2) is True


def test_no_target_is_never_treated_as_tripped():
    """没钉引擎/没 base_url ＝ 接受 hub 自选，本熔断压根不该介入。"""
    av.note_hub_synth_outcome("", "index_tts", timed_out=True, now=T0)
    av.note_hub_synth_outcome("http://hub", "", timed_out=True, now=T0)
    assert av.hub_synth_breaker_open("", "index_tts", now=T0) is False
    assert av.hub_synth_breaker_open("http://hub", "", now=T0) is False
    assert av.hub_synth_breaker_state("", "")["breaker"] == "closed"


def test_only_timeouts_feed_the_breaker_not_fast_failures():
    """契约钉死：喂熔断的判据是**时间**。

    HTTP 4xx/5xx 是几百毫秒的快败——没有延迟税可省，把它算进来会在「hub 答得很快
    只是拒绝了」时误开路（那时该做的是看错误码，不是跳过 hub 五分钟）。引擎冒名
    另有指纹闸处置。这里从**调用点**核对：合成路径只在 TimeoutError 分支喂它。
    """
    import inspect

    from src.ai.tts_pipeline import TTSPipeline

    src = inspect.getsource(TTSPipeline._try_hub_fish)
    src += "\n" + inspect.getsource(TTSPipeline._hub_fish_paced)
    lines = src.splitlines()
    # 两条路（整段单发 / 分段编排）都必须喂——分段路是本机生产主路，漏掉它熔断
    # 就永远看不到超时（正是本次实测流失的那条）
    assert src.count("_note_hub_timing") >= 4, "两条路各需 成功+超时 两处上报"

    hits = [i for i, ln in enumerate(lines) if "timed_out=True" in ln]
    assert len(hits) >= 2, "两条路各需一处超时上报"
    for i in hits:
        window = "\n".join(lines[max(0, i - 4):i + 1])
        assert "TimeoutError" in window, (
            "第 %d 行的超时上报没有被 TimeoutError 判定护住：\n%s" % (i, window))


def test_open_breaker_skips_hub_in_synthesis_path():
    """开路期必须**真的跳过** hub（省掉那 30 秒），而不只是记个数。"""
    import inspect

    from src.ai.tts_pipeline import TTSPipeline

    src = inspect.getsource(TTSPipeline._try_hub_fish)
    i_brk = src.index("hub_synth_breaker_open")
    i_paced = src.index("_hub_fish_paced(")
    # 熔断判定必须在真正发起合成（含分段编排）**之前**
    assert i_brk < i_paced, "熔断判定跑在合成之后＝那 30 秒照样白付"
    assert "timeout_breaker" in src, "必须可关（共享 hub 上运营可能另有安排）"
    assert "hub_synth_timing_out" in src, "跳过原因要落 rv.extra，否则无从归因"


def test_readonly_surfaces_never_steal_the_probe_slot():
    """只读面（看板/坐席预告）绝不许调 ``hub_synth_breaker_open``。

    半开态下那个函数**有副作用**：首个调用者取走唯一的探路名额。若看板轮询或
    坐席打开会话时调了它，恢复探测的机会就被从「真正要发语音的那一发」手里抢走
    ——表现是熔断永远半开、语音永远不恢复，而两处代码看起来都没错。
    """
    import inspect

    from src.web.routes import voice_routes

    # 只看**代码**行：被查文件会在注释里提这个函数名（正是为了警告后人别用），
    # 按裸子串扫会把说明文字本身当成违规。
    code = "\n".join(
        ln.split("#", 1)[0] for ln in inspect.getsource(voice_routes).splitlines())
    assert "hub_synth_breaker_open" not in code, (
        "坐席风险预告必须用只读的 hub_synth_breaker_state")
    assert "hub_synth_breaker_state" in code, "预告要与真实拒发行为同源"

    # 只读快照本身必须真的只读：连查 5 次不得改变状态
    for i in range(2):
        av.note_hub_synth_outcome(
            "http://hub", "index_tts", timed_out=True, now=T0 + i)
    later = T0 + av._BREAKER_OPEN_SEC + 10
    for _ in range(5):
        assert av.hub_synth_breaker_state(
            "http://hub", "index_tts", now=later)["breaker"] == "half_open"
    # 名额仍在 → 合成路径拿得到
    assert av.hub_synth_breaker_open("http://hub", "index_tts", now=later) is False


def test_timing_out_gets_its_own_error_code_not_the_generic_bucket():
    """「双绿却答不上来」必须自成一码。

    2026-08-22 的两小时潜伏就是通用码 hub_voice_source_unavailable 造成的：它把
    运维指向 /health（当时正绿）。本档更刁——目录 available:true **和** /health 200
    同时都绿，混进通用桶等于让人对着两个绿灯找原因。
    """
    from src.ai.tts_pipeline import HUB_SOURCE_ERROR_MARKERS, classify_voice_error

    assert "hub_synth_timing_out" in HUB_SOURCE_ERROR_MARKERS
    # 坐席能做的事与其他 hub 故障一样（换系统音色/等运维）→ 同一个可行动桶
    assert classify_voice_error("hub_synth_timing_out:index_tts") == "hub_source_down"


def test_strict_refusal_reports_timing_out_before_the_generic_code():
    """strict 拒发时，超时档的判定必须排在通用码之前，否则它永远出不来。"""
    import inspect

    from src.ai.tts_pipeline import TTSPipeline

    src = inspect.getsource(TTSPipeline._try_avatar_clone)
    i_to = src.index('hub_synth_timing_out')
    i_generic = src.index('_finalize_err("hub_voice_source_unavailable")')
    assert i_to < i_generic, "通用码兜在最后，超时档要先判"


def test_breaker_is_not_coupled_to_engine_identity_verification():
    """verify_engine 管「身份验不验」，熔断管「答不答得上来」——正交，别绑一起。

    绑在一起会让运维为排 hub 路由关掉指纹校验时，连超时保护一起丢掉（那正是
    最需要它的时候：正在出问题的引擎上）。
    """
    import inspect

    from src.ai.tts_pipeline import TTSPipeline

    src = inspect.getsource(TTSPipeline._try_hub_fish)
    line = next(ln for ln in src.splitlines()
                if "hub_synth_breaker_open" in ln or "timeout_breaker" in ln
                if "if " in ln)
    assert "verify_engine" not in line, (
        "熔断闸不该挂在 verify_engine 上：%s" % line.strip())


def test_operator_alert_names_the_double_green_trap():
    """告警必须点名「那两个绿灯别看」，否则运维会照旧去查目录然后放弃。"""
    from src.inbox.webhook_notifier import _build_message

    _t, txt = _build_message("voice_outage_alert", {
        "attempts": 12, "window_hours": 24, "consecutive_fails": 12,
        "top_reasons": {"hub_synth_timing_out:index_tts": 12},
        "last_ok_hours": 3.2})
    assert "index_tts" in txt
    assert "/health" in txt and "available:true" in txt   # 明确说这两处是绿的
    assert "gpu/overview" in txt                          # 指向真正该看的地方
    # 「过一会儿自己好了」的误判也要提前掐掉
    assert "探路" in txt


def test_every_hub_risk_value_is_renderable_by_the_agent_ui():
    """路由能吐的每个 hub_risk 都必须在收件箱里有分支 + 双语键。

    这是「绿灯说谎」的镜像故障：后端老实报了风险、前端没有对应分支 →
    dangerKey 为空 → 坐席看到的是**没有任何警示**，然后点发送吃一记拒发。
    2026-08-22 新增 timing_out 时就差这一步（路由先落地、UI 后补），本门禁
    把「后端加了风险值」与「坐席看得见」钉成同一件事。
    """
    import inspect
    import re
    from pathlib import Path

    from src.web.routes import voice_routes
    from src.web.web_i18n import get_translations

    code = "\n".join(
        ln.split("#", 1)[0] for ln in inspect.getsource(voice_routes).splitlines())
    # 赋给 hub_risk 的字面量（含经 _brk 中转的那一档）
    emitted = set(re.findall(r'hub_risk\s*=\s*"([a-z_]+)"', code))
    emitted |= set(re.findall(r'_brk\s*=\s*"([a-z_]+)"', code))
    emitted.discard("")
    assert {"timing_out", "engine_offline", "unreachable"} <= emitted, emitted

    tpl = (Path(inspect.getsourcefile(voice_routes)).parents[1]
           / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    branch = tpl[tpl.index("const dangerKey"):][:800]
    zh = get_translations("zh")
    en = get_translations("en")
    for risk in sorted(emitted):
        if risk == "recent_failures":
            continue          # 软风险，刻意不弹红条（只进状态条文案）
        assert "'%s'" % risk in branch, (
            "hub_risk=%s 后端会报但收件箱没有分支 → 坐席看不到警示" % risk)
        m = re.search(r"'%s'\s*\?\s*'([a-zA-Z0-9_.]+)'" % risk, branch)
        assert m, "hub_risk=%s 分支没接 i18n 键" % risk
        k = m.group(1)
        assert zh.get(k) and en.get(k), "%s 缺中英文案" % k
