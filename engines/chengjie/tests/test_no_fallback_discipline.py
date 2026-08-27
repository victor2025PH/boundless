"""无兜底纪律（2026-08-17 老板拍板，docs/实施33 v2）核心门禁。

三原则：① 链路失败=不发消息（禁一切静默替代品）② 失败必弹窗+ERROR 上报主机
③ 内容不丢（转工作台待发）。本文件钉：
  - delivery_block 模块契约（计数/快照/notify_host 出口）
  - true_probe 规格推导 + 连败状态机
  - 各拦截点的源码级接线（谁把兜底翻回来先红）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.ops import delivery_block as db
from src.ops.true_probe import build_probe_specs, next_strike_state

_SRC = Path(__file__).parent.parent / "src"


@pytest.fixture(autouse=True)
def _clean_counters():
    db.reset_for_tests()
    yield
    db.reset_for_tests()


# ── delivery_block 契约 ─────────────────────────────────────────────


def test_report_block_counts_and_snapshot(monkeypatch):
    calls = []

    def _fake_notify(title, message, *, key="", cooldown_sec=1800.0, **kw):
        calls.append((title, key, cooldown_sec))
        return True

    import src.utils.host_alert as ha
    monkeypatch.setattr(ha, "notify_host", _fake_notify)

    db.report_block("voice", reason="tts_failed", platform="telegram",
                    conversation_id="telegram:a:1", queued_draft=True)
    db.report_block("voice", reason="tts_failed")
    db.report_block("translate", reason="hold")

    snap = db.snapshot()
    assert snap["total"] == 3
    assert snap["by_domain"]["voice"] == 2
    assert snap["by_domain"]["translate"] == 1
    assert snap["by_reason"]["voice:tts_failed"] == 2
    assert snap["recent"][-1]["domain"] == "translate"
    # notify_host 每次都被调（去抖由 notify_host 自己的 cooldown 承担）
    assert len(calls) == 3
    # 2026-08-17 弹窗美化：标题从「AI 不可用」改「AI 回复未发出」——语义更准
    # （链路故障被拦下 ≠ 整个 AI 死了），纪律三原则（拦发/必响/内容不丢）不变。
    assert calls[0][0].startswith("AI 回复未发出")
    assert calls[0][1] == "deliv:voice"


def test_report_block_never_raises(monkeypatch):
    import src.utils.host_alert as ha

    def _boom(*a, **k):
        raise RuntimeError("popup broke")

    monkeypatch.setattr(ha, "notify_host", _boom)
    db.report_block("asr", reason="x")          # 不抛
    db.report_block("", reason="")               # 空域照记
    assert db.snapshot()["total"] == 2


def test_unknown_domain_tolerated():
    db.report_block("weird_domain", reason="r")
    assert db.snapshot()["by_domain"]["weird_domain"] == 1


# ── true_probe：规格推导 ────────────────────────────────────────────


_FULL_CFG = {
    "avatar_voice": {
        "enabled": True,
        "hub_fish": {
            "enabled": True,
            "base_url": "http://hub:9000",
            "tts_engine": "index_tts",
            "profile_map": {"lin_xiaoyu": "林小雨-智聊"},
        },
    },
    "translation": {"engines": {"ollama_mt": {
        "base_urls": ["http://mt:11434"], "model": "hy-mt2"}}},
    "vision": {"enabled": True, "base_url": "http://vl:11434/v1",
               "model": "qwen3-vl:8b"},
    "voice_recognition": {"enabled": True,
                          "base_url": "http://asr:8765/v1",
                          "model": "large-v3-turbo"},
}


def test_build_probe_specs_full_config():
    specs = build_probe_specs(_FULL_CFG)
    domains = [s["domain"] for s in specs]
    assert domains == ["tts", "translate", "vision", "asr"]
    tts = specs[0]
    assert tts["url"] == "http://hub:9000/api/tts_only"
    assert tts["json"]["tts_engine"] == "index_tts"   # 引擎显式钉，禁隐式路由
    assert tts["json"]["profile"] == "林小雨-智聊"
    assert tts["json"]["best_of"] == 1
    vis = specs[2]
    assert vis["url"] == "http://vl:11434/v1/chat/completions"
    assert vis["json"]["model"] == "qwen3-vl:8b"


def test_vision_probe_carries_real_bearer_for_cloud_endpoint():
    """识图切云后探针必须带真 Bearer（2026-08-27 03:59 实锤：裸打硅基 401 → 健康端点
    被探针判死攒 strike）；LAN/占位 key 保持旧行为（无 headers 字段=默认 'Bearer probe'）。"""
    cfg = dict(_FULL_CFG)
    cfg["vision"] = {"enabled": True, "base_url": "https://api.siliconflow.cn/v1",
                     "model": "Qwen/Qwen3-VL-8B-Instruct", "api_key": "sk-real-cloud-key"}
    vis = [s for s in build_probe_specs(cfg) if s["domain"] == "vision"][0]
    assert vis["headers"] == {"Authorization": "Bearer sk-real-cloud-key"}
    # LAN 占位 key（ollama）与未配 key：不带 headers=沿用默认 probe 头
    for k in ("ollama", "", None):
        cfg["vision"] = {"enabled": True, "base_url": "http://vl:11434/v1",
                         "model": "qwen3-vl:8b", **({"api_key": k} if k is not None else {})}
        vis = [s for s in build_probe_specs(cfg) if s["domain"] == "vision"][0]
        assert "headers" not in vis


def test_probe_image_fixture_is_structurally_valid():
    """识图探针的内嵌 PNG 必须结构完好且边长 >28px（2026-08-27 事故不变量）。

    旧常量 IDAT 的 CRC 是坏的、尾部缺 IEND：LAN ollama 解码宽容照单全收，探针长期
    绿；识图切硅基后严格校验每 10min 一个 400（code 20015 broken PNG file），连败
    弹窗。Qwen3-VL 另有 >28px 的硬性下限——合法的 8x8 同样被拒。两条都离线可验，
    不必等真打云端才发现。
    """
    import base64
    import binascii
    import struct

    from src.ops.true_probe import _RED_PNG_B64

    png = base64.b64decode(_RED_PNG_B64)
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "PNG 签名不对"

    tags, size, i = [], None, 8
    while i + 12 <= len(png):
        ln = struct.unpack(">I", png[i:i + 4])[0]
        tag = png[i + 4:i + 8]
        assert i + 12 + ln <= len(png), f"chunk {tag!r} 越界＝文件被截断"
        data = png[i + 8:i + 8 + ln]
        stored = struct.unpack(">I", png[i + 8 + ln:i + 12 + ln])[0]
        assert stored == binascii.crc32(tag + data) & 0xFFFFFFFF, \
            f"chunk {tag!r} CRC 损坏——严格解码器（硅基）会 400"
        if tag == b"IHDR":
            size = struct.unpack(">II", data[:8])
        tags.append(tag)
        i += 12 + ln
    assert i == len(png), "尾部有残留字节"
    assert tags[0] == b"IHDR" and tags[-1] == b"IEND", f"chunk 序列不完整：{tags}"
    # 已知厂商地板 28px；这里钉 64 是**留余量**——谁把夹具缩回紧贴地板先红。
    assert size and min(size) >= 64, f"边长须 >=64px（28 是厂商地板，留余量），实际 {size}"


def test_probe_surfaces_http_error_body():
    """4xx 必须带厂商响应体：'HTTP Error 400: Bad Request' 零信息量，排查全靠它。"""
    src = (_SRC / "ops" / "true_probe.py").read_text(encoding="utf-8")
    assert "except urllib.error.HTTPError" in src
    assert "ex.read()" in src


def test_content_verdict_rejects_answers_that_prove_nothing():
    """「有回话」不算活：openai_chat 两域必须做内容级弱断言（2026-08-27 补）。

    旧判据只查非空 → VLM 丢视觉塔/图被服务端丢弃后照样闲聊、MT 把源文原样回吐，
    全都一路绿灯。断言方向刻意保守：真跑对了几乎不可能不满足（探针误红代价最高）。
    """
    from src.ops.true_probe import content_verdict

    vision = {"expect_any": ["红", "red", "赤"]}
    for good in ("这张图的主色调是鲜艳的红色。", "The dominant color is RED.", "赤です"):
        assert content_verdict(good, vision) == (True, ""), good
    for blind in ("这张图片看起来很有趣。", "抱歉，我无法看到图片。", "主色调是蓝色。"):
        ok, why = content_verdict(blind, vision)
        assert not ok and "misses" in why, blind
    assert content_verdict("", vision)[0] is False        # 空回答仍是最先拦的

    mt = {"expect_latin": True}
    assert content_verdict("Hello, the weather is nice today.", mt) == (True, "")
    for bad, mark in (("你好，今天天气不错", "no latin"),          # 源文原样回吐
                      ("Hello 今天天气不错", "source echoed")):    # 半译混排
        ok, why = content_verdict(bad, mt)
        assert not ok and mark in why, bad

    # 无断言字段的 spec 保持旧语义（非空即过）——两条判据都是 opt-in，不波及他域。
    assert content_verdict("whatever", {}) == (True, "")


def test_true_probe_metrics_wired():
    """metrics 接线钉住（同 test_entrance_slo_metrics_wired 的站内惯例）：接线丢了
    观测面会**静默消失**而不是报错——此前 src/web 里一处 true_probe 引用都没有，
    「探针停摆」完全不可观测。"""
    src = (_SRC / "web" / "routes" / "drafts_routes.py").read_text(encoding="utf-8")
    assert "from src.ops.true_probe import metrics_snapshot" in src
    assert 'metrics["true_probe"]' in src


def test_probe_specs_carry_content_assertions():
    """断言必须真的挂在 spec 上——纯函数写好了没接线是最常见的静默失效。"""
    specs = {s["domain"]: s for s in build_probe_specs(_FULL_CFG)}
    assert "红" in specs["vision"]["expect_any"]
    assert specs["translate"]["expect_latin"] is True
    assert "今天" in specs["asr"]["expect_any"]
    # tts 验的是 magic bytes + 引擎归属，不该被误挂文本断言
    assert "expect_any" not in specs["tts"] and "expect_latin" not in specs["tts"]


def test_asr_expect_tokens_calibrated_against_known_bad_sample():
    """ASR 断言词表必须能判死已知坏样本——词表不是拍脑袋选的，是被反例逼出来的。

    2026-08-27：夹具 WAV 头把 22050 写成 44100，音频 2 倍速播放，ASR 十天来一直转成
    「挺好 挺好 挺好 挺挺挺不錯」而探针照报 ok。注意坏样本**含**「不錯」——所以
    不错/不錯 绝不能进词表（它连坏样本都满足＝零鉴别力）。
    """
    from src.ops.true_probe import _ASR_EXPECT, content_verdict

    spec = {"expect_any": list(_ASR_EXPECT)}
    assert content_verdict("嗯嗯今天天氣不錯啊", spec) == (True, "")     # 修正后真机输出
    assert content_verdict("你好你好，今天天气不错", spec) == (True, "")  # 简体亦可
    bad, why = content_verdict("挺好 挺好 挺好 挺挺挺不錯", spec)        # 2 倍速那版
    assert not bad and "misses" in why
    assert not any(k in ("不错", "不錯") for k in _ASR_EXPECT), \
        "不错/不錯 对已知坏样本无鉴别力，不得进词表"
    ok, why = content_verdict("", spec)
    assert not ok and "empty" in why


def test_asr_fixture_is_canonical_16k_wav():
    """ASR 夹具必须是规范的 16k 单声道 PCM WAV（2026-08-27 事故不变量）。

    与识图那张坏 PNG 同一病根：夹具从没被验过。这里离线校 RIFF/WAVE 结构、
    chunk 链完整、采样率与声道数——「头里写的」和「实际是的」不一致时，
    音频会以错误速度播放而探针只会看到一串非空文字。
    """
    import struct

    from src.ops.true_probe import ASR_FIXTURE

    if not ASR_FIXTURE.is_file():
        pytest.skip("本机无 ASR 夹具（*.wav 未进版本控制，见下条门禁）")
    b = ASR_FIXTURE.read_bytes()
    assert b[:4] == b"RIFF" and b[8:12] == b"WAVE", "不是 WAV（媒体产物必验 magic）"
    assert struct.unpack("<I", b[4:8])[0] == len(b) - 8, "RIFF 长度与文件实际不符"

    rate = channels = data_len = None
    i = 12
    while i + 8 <= len(b):
        cid, ln = b[i:i + 4], struct.unpack("<I", b[i + 4:i + 8])[0]
        assert i + 8 + ln <= len(b), f"chunk {cid!r} 越界＝文件被截断"
        if cid == b"fmt ":
            _, channels, rate = struct.unpack("<HHI", b[i + 8:i + 16])
        elif cid == b"data":
            data_len = ln
        i += 8 + ln + (ln & 1)
    assert rate == 16000, f"须 16kHz（whisper 原生），实际 {rate}"
    assert channels == 1, f"须单声道，实际 {channels}"
    assert data_len and 1.0 < data_len / (rate * channels * 2) < 15.0, "时长不合理"


def test_probe_gap_reasons_separates_disabled_from_blind_spot():
    """「域被关掉」与「域开着却探不了」必须分开——混为一谈就是误报（2026-08-27）。

    真机首跑即自撞一次：zhiliao_pilot 的 config.yaml 自带一段**空**的 ollama_mt
    脚手架（base_url/model 皆空、order 里根本没有它），旧判据「配置里有这个键就
    算启用」把它报成盲区。translate 没有 enabled 开关，唯一可信的启用信号是
    「它在不在引擎链里」。
    """
    from src.ops.true_probe import probe_gap_reasons

    # ① 全关的实例：一个盲区都不该报
    assert probe_gap_reasons({
        "vision": {"enabled": False},
        "voice_recognition": {"enabled": False},
        "avatar_voice": {"enabled": False},
        "translation": {"engines": {
            "order": ["ai"],
            "ollama_mt": {"base_url": "", "model": "", "api_key": "ollama"},
        }},
    }) == {}

    # ② 开着却缺配置：必须点名，且原因要具体到能照着修
    gaps = probe_gap_reasons({
        "vision": {"enabled": True, "base_url": "http://vl/v1"},          # 缺 model
        "voice_recognition": {"enabled": True},                            # 缺 base_url
        "translation": {"engines": {"order": ["ollama_mt"],
                                    "ollama_mt": {"model": "hy-mt2"}}},    # 缺 base_url
    })
    assert set(gaps) == {"vision", "translate", "asr"}
    assert "model" in gaps["vision"] and "base_url" in gaps["asr"]
    assert "引擎链" in gaps["translate"]

    # ③ 只在 per_lang_order 里被用到，同样算在链上
    assert "translate" in probe_gap_reasons({"translation": {"engines": {
        "per_lang_order": {"hi": ["ai", "ollama_mt"]},
        "ollama_mt": {"model": "hy-mt2"}}}})

    # ④ 配置齐全 → 无盲区（与 build_probe_specs 同一事实源，不得各算一套）
    assert probe_gap_reasons(_FULL_CFG) == {}


def test_selfcheck_cli_contract(tmp_path, monkeypatch):
    """自检 CLI 的两条契约：① 只读 ② 「无规格」与「探针失败」必须分开报。

    2026-08-27 事故里最贵的一段是「改完配置无法当场验一轮，只能等 10 分钟 tick +
    看一句零信息量的弹窗」。工具没门禁会烂掉，这里钉住它的核心区分。
    """
    import importlib.util

    path = Path(__file__).parent.parent / "tools" / "true_probe_selfcheck.py"
    spec = importlib.util.spec_from_file_location("_tp_selfcheck", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 空配置的根：域全关 → 归 disabled 而非盲区，且**一次网络调用都不发**
    monkeypatch.setattr(mod, "load_merged_config", lambda root: {})
    monkeypatch.setattr(mod, "run_probe",
                        lambda s: pytest.fail("空配置不该发起任何探测"))
    rep = mod.run_root(tmp_path, ["vision", "asr"], 1)
    assert rep["specs"] == [] and rep["results"] == []
    assert set(rep["disabled_domains"]) == {"vision", "asr"} and rep["gaps"] == {}

    # 有规格但失败：failures 有值，盲区为空——两种病不能混成一个数字
    monkeypatch.setattr(mod, "load_merged_config", lambda root: _FULL_CFG)
    monkeypatch.setattr(mod, "run_probe", lambda s: (False, "HTTP 400: nope"))
    rep = mod.run_root(tmp_path, ["vision"], 2)
    assert rep["gaps"] == {} and len(rep["failures"]) == 2
    assert "含 红" in rep["specs"][0]["assertion"]        # 断言必须对人可见

    # 默认选域＝「除 tts 全跑」而非白名单：tts 真烧 hub GPU 须显式 --all；而后加的
    # 域（识图备胎）必须默认被覆盖——白名单会让新能力默认漏检，那是探针最爱长的盲区。
    assert mod.selected("vision", [], False) is True
    assert mod.selected("vision_backup1", [], False) is True
    assert mod.selected("tts", [], False) is False
    assert mod.selected("tts", [], True) is True
    assert mod.selected("vision", ["asr"], False) is False   # 显式 --domain 优先


def test_vision_probe_budget_follows_production_not_a_second_hardcoded_number():
    """探针的「多久算太久」必须跟生产同一个数——不许在探针里再写一个。

    2026-08-27 老板定线「20 秒不识图就是有问题，要反馈」，落在生产 vision 配置上
    （LAN 5s ＋ 云端 15s ＝ 端到端 20s）。探针若自留 45s 硬编码，就会出现最难查的
    分裂：**生产已按 15s 掐断并失败，探针慢悠悠等到 45s 报绿**——看板说健康、客户
    在挨等，而且谁调了生产超时都不会想到还有第二个数要跟着改。
    """
    from src.ops.true_probe import (
        _VISION_PROBE_FALLBACK_TIMEOUT,
        _vision_endpoint_timeout,
    )

    vi = {
        "enabled": True, "model": "qwen3-vl:8b",
        "base_urls": ["http://192.168.0.176:11434/v1",
                      "https://api.siliconflow.cn/v1"],
        "endpoint_timeouts": {"192.168.0.176": 5, "siliconflow": 15},
        "timeout": 20,
    }
    # ① 逐端点跟随生产（快主路 5s／慢备胎 15s，不是一个笼统的大数）
    assert _vision_endpoint_timeout(vi, "http://192.168.0.176:11434/v1") == 5.0
    assert _vision_endpoint_timeout(vi, "https://api.siliconflow.cn/v1") == 15.0
    # ② 未列出的端点吃全局，同样跟随生产
    assert _vision_endpoint_timeout(vi, "https://other.example/v1") == 20.0
    # ③ 端到端预算＝顺序试的和，必须守住老板给的 20s
    total = sum(_vision_endpoint_timeout(vi, u) for u in vi["base_urls"])
    assert total <= 20.0, f"端到端 {total}s 超过 20s 线"
    # ④ 配置推不出时才回落硬编码（旧行为零破坏）
    assert _vision_endpoint_timeout({}, "http://x/v1") == _VISION_PROBE_FALLBACK_TIMEOUT

    # ⑤ 真进 spec：build_probe_specs 出来的 timeout 就是上面这些值，
    #    不是另一处 45.0 —— 单元函数对了而接线没换是这类修复最常见的半途而废。
    specs = {s["domain"]: s for s in build_probe_specs(dict(_FULL_CFG, vision=vi))}
    assert specs["vision"]["timeout"] == 5.0
    assert specs["vision_backup1"]["timeout"] == 15.0

    # ⑥ ratchet：vision spec 里不许再出现字面超时（第二事实源的复发点）
    src = (Path(__file__).parent.parent / "src" / "ops"
           / "true_probe.py").read_text(encoding="utf-8")
    block = src[src.index('"domain": "vision" if'):]
    block = block[:block.index("# ── asr")]
    assert '"timeout": 4' not in block and '"timeout": 3' not in block, \
        "vision spec 的 timeout 必须由 _vision_endpoint_timeout 推，别写死"


def _load_selfcheck():
    import importlib.util

    path = Path(__file__).parent.parent / "tools" / "true_probe_selfcheck.py"
    spec = importlib.util.spec_from_file_location("_tp_selfcheck", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_selfcheck_from_state_never_probes_and_distrusts_stale(tmp_path, monkeypatch):
    """``--from-state`` 三条契约：不探测 ／ 陈旧不当绿 ／「没文件」分两种。

    这个模式的全部价值就是「问一句生产健不健康不用付一次真推理」——一旦它偷偷发探测，
    价值归零。而它最容易犯的错是把**冻住的旧读数**当健康：watchdog 死掉后状态文件
    原地不动，四域永远显示上一轮的绿，「绿了但没在更新」和「真的绿」长得一模一样。
    同日在 ``line_rpa._classify_alerts`` 上认出的是同一个陷阱（告警源哑了＝表里不再
    进新行＝看起来一切正常），所以这里必须钉死。
    """
    import json as _json
    import time as _time

    mod = _load_selfcheck()
    monkeypatch.setattr(
        mod, "run_probe", lambda s: pytest.fail("--from-state 不得发起任何探测"))

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()

    # ① 没有状态文件 + 该实例压根没有可探的域（运营四域全关）＝正常，不许报红。
    #    这是首版真踩过的误报：对着一台合规实例长年喊红，喊几次就没人看退出码了。
    monkeypatch.setattr(mod, "load_merged_config", lambda root: {})
    rep = mod.read_root_state(tmp_path, [])
    assert rep["present"] is False and rep["nothing_to_probe"] is True
    assert mod.main(["--from-state", "--data-root", str(tmp_path)]) == 0

    # ② 没有状态文件 + 域配着＝watchdog 没在写，是真问题
    monkeypatch.setattr(mod, "load_merged_config", lambda root: _FULL_CFG)
    rep = mod.read_root_state(tmp_path, [])
    assert rep["present"] is False and rep["nothing_to_probe"] is False
    assert mod.main(["--from-state", "--data-root", str(tmp_path)]) == 2

    # ③ 新鲜且全绿 → 0
    fresh = {
        "updated_ts": _time.time(), "updated_at": "now",
        "domains": {"vision": {"ok": True, "fails": 0, "detail": "0.3s 红"}},
        "gaps": {},
    }
    (cfg_dir / "true_probe_state.json").write_text(
        _json.dumps(fresh), encoding="utf-8")
    assert mod.main(["--from-state", "--data-root", str(tmp_path)]) == 0

    # ④ 同样一份**全绿**读数，只是老了几个钟头 → 必须报红。域仍显示 ok=True，
    #    退出码却是 2——这正是「不拿冻住的绿冒充健康」的可执行定义。
    stale = dict(fresh, updated_ts=_time.time() - 6 * 3600)
    (cfg_dir / "true_probe_state.json").write_text(
        _json.dumps(stale), encoding="utf-8")
    rep = mod.read_root_state(tmp_path, [])
    assert rep["domains"]["vision"]["ok"] is True      # 读数本身还是绿的
    assert rep["stalled"]                              # 但判定是停摆
    assert mod.main(["--from-state", "--data-root", str(tmp_path)]) == 2

    # ⑤ 陈旧判据不自己造一套：与 watchdog 告警共用 stalled_verdict，
    #    两边各算一套就会出现「工具说停摆、告警说正常」的互相打脸。
    src = (Path(__file__).parent.parent / "tools"
           / "true_probe_selfcheck.py").read_text(encoding="utf-8")
    assert "stalled_verdict" in src
    assert "STATE_MAX_AGE" not in src, "别在 CLI 里复制一份陈旧阈值"


def test_build_probe_specs_skips_disabled_domains():
    cfg = {
        "avatar_voice": {"enabled": False},
        "translation": {},
        "vision": {"enabled": False},
        "voice_recognition": {"enabled": False},
    }
    assert build_probe_specs(cfg) == []
    assert build_probe_specs({}) == []


def test_tts_probe_timeout_exceeds_hub_retry_budget():
    """tts 探针超时必须留够 hub 内部换引擎/换副本的时间（2026-08-21 事故不变量）。

    探针提前放弃 → 在途请求仍挂在 hub 上 → 副本被判「疑似挂死」并被后续路由
    规避＝探针反伤业务链。实测 hub 内部预算 100-120s，故地板 60s、默认 90s，
    且运营配得再小也不生效。
    """
    assert build_probe_specs(_FULL_CFG)[0]["timeout"] >= 90.0

    def _tts_timeout(hub_extra):
        cfg = dict(_FULL_CFG)
        hf = dict(_FULL_CFG["avatar_voice"]["hub_fish"])
        hf.update(hub_extra)
        cfg["avatar_voice"] = {"enabled": True, "hub_fish": hf}
        return build_probe_specs(cfg)[0]["timeout"]

    # 业务侧 timeout_sec 短是刻意的（坐席等不了），不得把探针一起拖短
    assert _tts_timeout({"timeout_sec": 30.0}) >= 90.0
    assert _tts_timeout({"probe_timeout_sec": 150.0}) == 150.0
    assert _tts_timeout({"probe_timeout_sec": 5.0}) == 60.0      # 地板兜住
    assert _tts_timeout({"probe_timeout_sec": "bad"}) >= 90.0    # 脏值回默认


def test_build_probe_specs_hub_without_profile_skipped():
    cfg = dict(_FULL_CFG)
    cfg["avatar_voice"] = {
        "enabled": True,
        "hub_fish": {"enabled": True, "base_url": "http://hub:9000",
                     "profile_map": {}},
    }
    assert "tts" not in [s["domain"] for s in build_probe_specs(cfg)]


# ── true_probe：连败状态机 ──────────────────────────────────────────


def test_strike_state_alert_once_then_recover():
    st = {}
    st, a1 = next_strike_state(st, "tts", False, fail_strikes=2, now=1.0)
    assert a1 == ""                       # 第 1 败：不响（吸收抖动）
    st, a2 = next_strike_state(st, "tts", False, fail_strikes=2, now=2.0)
    assert a2 == "alert"                  # 第 2 败：首报
    st, a3 = next_strike_state(st, "tts", False, fail_strikes=2, now=3.0)
    assert a3 == ""                       # 持续失败不重复出 action
    st, a4 = next_strike_state(st, "tts", True, fail_strikes=2, now=4.0)
    assert a4 == "recovered"              # 报过警后恢复：绿窗
    st, a5 = next_strike_state(st, "tts", True, fail_strikes=2, now=5.0)
    assert a5 == ""                       # 正常运行无动作
    assert st["tts"]["fails"] == 0


def test_strike_state_domains_independent():
    st = {}
    st, _ = next_strike_state(st, "tts", False, fail_strikes=2, now=1.0)
    st, a = next_strike_state(st, "asr", False, fail_strikes=1, now=1.0)
    assert a == "alert"                   # asr 阈值 1 独立触发
    assert st["tts"]["fails"] == 1 and not st["tts"]["alerted"]


def test_strike_state_survives_restart(tmp_path):
    """连败态必须跨重启存活——否则同一个问题每次重启弹一次窗（2026-08-27 实录：
    识图红了一整天，04:48/05:28/06:18 三次重启各弹一次）。"""
    from src.ops import true_probe as tp

    p = tp.state_path(tmp_path)
    st = {}
    st, _ = tp.next_strike_state(st, "vision", False, fail_strikes=2, now=100.0)
    st, action = tp.next_strike_state(st, "vision", False, fail_strikes=2, now=200.0)
    assert action == "alert"                       # 首报
    assert tp.write_state(p, st, observations={
        "vision": {"ok": False, "detail": "HTTP 400: broken PNG"}}, now=200.0)

    # 重启：从文件恢复 → 同一个还没修好的问题**不再重复弹窗**
    restored = tp.restore_strike_state(p, now=260.0)
    assert restored["vision"]["alerted"] is True and restored["vision"]["fails"] == 2
    _, action = tp.next_strike_state(restored, "vision", False,
                                     fail_strikes=2, now=300.0)
    assert action == ""
    # 真恢复了才补绿窗
    _, action = tp.next_strike_state(restored, "vision", True,
                                     fail_strikes=2, now=400.0)
    assert action == "recovered"

    # 陈旧状态一律丢弃：别把「防弹窗风暴」做成「永久静音」
    assert tp.restore_strike_state(p, now=200.0 + tp.STATE_MAX_AGE_SEC + 1) == {}
    # 读不到/坏 JSON → 空态，绝不抛
    assert tp.restore_strike_state(tmp_path / "nope.json") == {}
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert tp.restore_strike_state(tmp_path / "bad.json") == {}


def test_probe_stall_detection_has_no_false_positives(tmp_path, monkeypatch):
    """探针**自己停摆**必须被发现，且构造上零误报（2026-08-27 自省闭环）。

    当天我自己漏了一个局部 import → `_check_true_probes` 整段抛异常被 tick 吞掉 →
    探针不跑而日志零正面痕迹。判据只认「状态文件存在且陈旧」：文件存在＝这台机器上
    它跑通过，陈旧＝它停了；从没跑过刻意不报（那是未启用/刚部署，报了就是误报——
    误报的代价今天已经付过一次）。
    """
    import types

    from src.inbox.health_watchdog import HealthWatchdog
    from src.ops import true_probe as tp

    # ① 从没跑过（无状态文件）→ 永不报
    assert tp.stalled_verdict(tmp_path, interval_min=10, now=1e9) is None

    tp.write_state(tp.state_path(tmp_path), {"vision": {"fails": 0}},
                   observations={"vision": {"ok": True}}, now=1000.0)
    # ② 刚跑过 → 不报；③ 超过 3× 周期 → 报，且给出可照着查的数字
    assert tp.stalled_verdict(tmp_path, interval_min=10, now=1500.0) is None
    v = tp.stalled_verdict(tmp_path, interval_min=10, now=1000.0 + 1900.0)
    assert v and v["stale_sec"] == 1900 and v["threshold_sec"] == 1800

    # ④ watchdog 侧真调用：停摆 → 告警一次；恢复 → 补绿窗并清零
    sent: List[str] = []
    monkeypatch.setattr("src.utils.host_alert.notify_host",
                        lambda t, b, **k: sent.append(t) or True)
    cfg = {"health_watchdog": {"true_probe": {"enabled": True, "interval_min": 10}}}
    fake = types.SimpleNamespace(
        _config_manager=types.SimpleNamespace(
            config=cfg, config_path=str(tmp_path / "config.yaml")))
    HealthWatchdog._check_probe_stalled(fake, now=1000.0 + 1900.0)
    HealthWatchdog._check_probe_stalled(fake, now=1000.0 + 2000.0)   # 不重复轰
    assert sent == ["真活探针停摆"], sent
    tp.write_state(tp.state_path(tmp_path), {"vision": {"fails": 0}}, now=3000.0)
    HealthWatchdog._check_probe_stalled(fake, now=3010.0)
    assert sent[-1] == "真活探针已恢复运行"

    # ⑤ 总开关关着 → 全程静默（未启用部署不该收到任何探针噪音）
    sent.clear()
    fake2 = types.SimpleNamespace(_config_manager=types.SimpleNamespace(
        config={"health_watchdog": {"true_probe": {"enabled": False}}},
        config_path=str(tmp_path / "config.yaml")))
    HealthWatchdog._check_probe_stalled(fake2, now=1e9)
    assert sent == []


def test_metrics_snapshot_reads_state_never_probes(tmp_path):
    """观测面只读状态文件——此前 src/web 里搜不到一处 true_probe 引用，
    「探针停摆」完全不可观测（stale_sec 就是为区分它与「四域都绿」）。"""
    from src.ops import true_probe as tp

    assert tp.metrics_snapshot(tmp_path) == {"present": False}   # 没跑过如实说

    st = {"vision": {"fails": 2, "alerted": True, "last_ok": 0.0, "last_run": 500.0},
          "asr": {"fails": 0, "alerted": False, "last_ok": 500.0, "last_run": 500.0}}
    tp.write_state(tp.state_path(tmp_path), st, observations={
        "vision": {"ok": False, "detail": "HTTP 400: nope"},
        "asr": {"ok": True, "detail": "0.4s"}},
        gaps={"translate": "ollama_mt 在引擎链里但缺 base_url(s) 或 model"}, now=500.0)

    snap = tp.metrics_snapshot(tmp_path, now=560.0)
    assert snap["present"] is True and snap["stale_sec"] == 60
    assert snap["failing"] == ["vision"]
    assert snap["domains"]["vision"]["alerted"] is True
    assert snap["domains"]["asr"]["ok"] is True
    assert "translate" in snap["gaps"]


def test_backup_endpoint_specs_observe_without_popup():
    """识图备胎：逐端点探但**不弹窗**（它坏了不影响当下出话，半夜弹窗是纯噪音），
    且端点顺序/模型名必须复用 vision_client（探针不能测的是另一条路）。"""
    cfg = dict(_FULL_CFG)
    cfg["vision"] = {
        "enabled": True, "model": "Cloud/VL-8B",
        "base_urls": ["https://api.siliconflow.cn/v1", "http://192.168.0.176:11434/v1"],
        "endpoint_models": {"192.168.0.176": "qwen3-vl:8b-instruct"},
    }
    vis = [s for s in build_probe_specs(cfg) if s["domain"].startswith("vision")]
    assert [s["domain"] for s in vis] == ["vision", "vision_backup1"]
    # 模型名解析**委派**给 vision_client（唯一事实源），这里只钉委派关系——别在这里
    # 复刻它的映射表断言：那会让本测试依赖 vision_client 侧的未装载改动，HEAD 上
    # 回落全局 model 就红（2026-08-27 临时 worktree 检出 HEAD 实锤，正是它抓到的）。
    from src.ops.true_probe import _vision_endpoint_model
    for spec, url in zip(vis, cfg["vision"]["base_urls"]):
        assert spec["json"]["model"] == _vision_endpoint_model(cfg["vision"], url)
    assert vis[0].get("alert", True) is True
    assert vis[1]["alert"] is False
    # watchdog 必须尊重该标记（源码级钉：翻回来就会半夜为备胎弹窗）
    src = (_SRC / "inbox" / "health_watchdog.py").read_text(encoding="utf-8")
    assert 'if not spec.get("alert", True):' in src
    assert "restore_strike_state" in src and "write_state" in src


def test_watchdog_probe_tick_actually_runs(tmp_path, monkeypatch):
    """**真调用** _check_true_probes 一轮——源码级断言抓不到运行时导入错误。

    2026-08-27 实锤：接线漏了一个函数内 `from pathlib import Path`（本模块无顶层
    pathlib），NameError 被 tick 外层的 DEBUG except 吞掉 → 探针从重启起整段不跑、
    日志零痕迹，只有「轮次完成那行不再出现」这一个极易忽略的负面信号。前面那些
    源码字符串断言全绿，照样放它上生产。所以这条必须真跑。
    """
    import types

    from src.inbox.health_watchdog import HealthWatchdog
    from src.ops import true_probe as tp

    calls: List[str] = []

    def _stub_probe(spec):
        calls.append(str(spec.get("domain")))
        return True, "0.1s stub"

    monkeypatch.setattr(tp, "run_probe", _stub_probe)   # 零网络零 GPU

    cfg = dict(_FULL_CFG)
    cfg["health_watchdog"] = {"true_probe": {"enabled": True, "fail_strikes": 2}}
    fake = types.SimpleNamespace(
        _config_manager=types.SimpleNamespace(
            config=cfg, config_path=str(tmp_path / "config.yaml")),
        _tp_last_run=0.0,
        _tp_state=None,
    )
    HealthWatchdog._check_true_probes(fake, now=1000.0)

    assert "vision" in calls and "asr" in calls, f"探针没真跑：{calls}"
    # 状态文件必须落在配置目录（跨重启去抖 + metrics 的唯一数据源）
    snap = tp.metrics_snapshot(tmp_path, now=1000.0)
    assert snap["present"] is True and snap["failing"] == []
    assert set(snap["domains"]) == set(calls)


def test_recover_without_prior_alert_is_silent():
    st = {}
    st, _ = next_strike_state(st, "vision", False, fail_strikes=3, now=1.0)
    st, a = next_strike_state(st, "vision", True, fail_strikes=3, now=2.0)
    assert a == ""                        # 没报过警的抖动恢复不发绿窗（防噪）


# ── 各拦截点源码级接线钉（兜底翻回来先红）────────────────────────────


def test_ai_client_chat_fallback_gate_wired():
    src = (_SRC / "ai" / "ai_client.py").read_text(encoding="utf-8")
    assert "_fb_chat_fallback_enabled" in src
    assert "cloud_failed_no_fallback" in src
    # 闸必须只拦兜底身份，不拦本地主链（as_primary）
    i = src.index("cloud_failed_no_fallback")
    assert "not as_primary" in src[i - 600: i]


def test_autosend_worker_no_original_on_translate_failure():
    src = (_SRC / "inbox" / "autosend_worker.py").read_text(encoding="utf-8")
    assert "出站翻译异常，发原文" not in src
    assert "人工通过出站翻译异常，发原文" not in src
    assert src.count("translate_hold") >= 2   # 两条投递路径都走 HOLD


def test_outbound_translate_all_failures_hold():
    src = (_SRC / "inbox" / "outbound_translate.py").read_text(encoding="utf-8")
    assert "翻译调用失败，发原文" not in src
    assert "_report_block_safe" in src
    assert "_TRANSLATABLE_RE" in src           # 纯 emoji 放行护栏在场


def test_asr_failure_skips_auto_reply():
    src = (_SRC / "client" / "telegram_client.py").read_text(encoding="utf-8")
    assert "语音没听懂就不装懂" in src
    i = src.index("语音没听懂就不装懂")
    seg = src[i: i + 2600]
    assert 'report_block(' in seg and '"asr"' in seg
    assert "跳过自动回复" in seg


def test_promise_fail_excuse_removed():
    src = (_SRC / "skills" / "skill_manager.py").read_text(encoding="utf-8")
    assert '"promise_fail"' not in src         # 「手机抽风改天补」话术出口拆除
    assert "promise_fulfill_failed" in src     # 改为 delivery_block 上报


def test_sender_voice_block_queues_draft():
    src = (_SRC / "client" / "sender.py").read_text(encoding="utf-8")
    assert "voice_blocked" in src              # 拦下的回复转工作台待发
    assert "upsert_draft" in src


def test_seat_banner_only_recent_failures():
    db.report_block("vision", reason="no_desc")
    now = 10_000.0
    # 把这次失败改成超过 30min 的旧账
    with db._lock:
        db._last_ts["vision"] = now - 2000
        db._recent[-1]["ts"] = now - 2000
    stale = db.seat_banner(now=now, max_age_sec=1800)
    assert stale["active"] is False
    assert stale["by_domain"] == {}

    db.report_block("asr", reason="empty")
    fresh = db.seat_banner(now=now + 1, max_age_sec=1800)
    assert fresh["active"] is True
    assert "asr" in fresh["by_domain"]
    assert "vision" not in fresh["by_domain"]


def test_vision_single_track_no_ocr_fallback():
    src = (_SRC / "client" / "telegram_client.py").read_text(encoding="utf-8")
    i = src.index("async def _get_image_content")
    seg = src[i: i + 1200]
    assert "不再" in seg and "OCR" in seg
    assert "ImageRecognizer" not in seg
    assert "没看懂图就不装懂" in src
    hold = src[src.index("没看懂图就不装懂"): src.index("没看懂图就不装懂") + 1800]
    assert 'report_block(' in hold and '"vision"' in hold
    assert "跳过自动回复" in hold


async def test_vision_zhipu_fallback_hard_gate(monkeypatch):
    """no_cloud_fallback=true 时智谱回落结构性关闭——即使有人贴回 key 也不触碰。"""
    from src import vision_client as vcm

    monkeypatch.setattr(vcm.VisionClient, "initialize", lambda self: False)

    def _boom(*a, **k):  # 硬闸生效 = 凭据函数根本不该被咨询
        raise AssertionError("zhipu credentials consulted despite hard gate")

    monkeypatch.setattr(vcm, "_zhipu_credentials", _boom)
    merged = {
        "provider": "openai_compatible",
        "base_url": "http://127.0.0.1:1/v1",
        "model": "x",
        "no_cloud_fallback": True,
    }
    gv = {"zhipu_api_key": "sk-someone-pasted-a-key-back"}
    txt, dbg = await vcm.VisionClient._describe_fallback_chain(
        merged, gv, "nonexistent.jpg")
    assert txt is None
    assert dbg.endswith("no_cloud_fallback")


def test_vision_hard_gate_overlay_enabled():
    """zhiliao 生产 overlay 必须开着硬闸（防止将来被顺手删掉）。"""
    overlay = Path(r"D:\chengjie-instances\zhiliao\data\config\config.local.yaml")
    if not overlay.exists():
        pytest.skip("非生产机，无 zhiliao overlay")
    text = overlay.read_text(encoding="utf-8")
    assert "no_cloud_fallback: true" in text


def test_protocol_and_autodraft_hold_unrecognized_media():
    proto = (_SRC / "integrations" / "protocol_autoreply.py").read_text(
        encoding="utf-8")
    assert "图片未看懂 → 跳过自动回复" in proto
    assert "语音未转写 → 跳过自动回复" in proto
    ad = (_SRC / "inbox" / "autodraft_helpers.py").read_text(encoding="utf-8")
    assert 'decided_by="vision_hold"' in ad
    assert 'decided_by="asr_hold"' in ad


def test_workbench_delivery_block_wired():
    setup = (_SRC / "web" / "routes" / "unified_inbox_setup_routes.py").read_text(
        encoding="utf-8")
    assert "seat_banner" in setup and '"delivery_block"' in setup
    drafts = (_SRC / "web" / "routes" / "drafts_routes.py").read_text(
        encoding="utf-8")
    assert 'metrics["delivery_block"]' in drafts
    html = (_SRC / "web" / "templates" / "workspace_base.html").read_text(
        encoding="utf-8")
    # 实施75 batch2：顶部红条退役，拦截提示走右下胶囊+卡片（AITRNotify）
    assert "AITRNotify.ongoing.set('delivblock'" in html
    assert "_renderDelivBlock" in html
    assert "ws.delivblock.text_media" in html
    api = (_SRC / "web" / "templates" / "_api_fetch.html").read_text(
        encoding="utf-8")
    assert "ws-delivblock" in api
