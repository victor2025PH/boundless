"""#169 出站媒体体积上限按平台（2026-09-05 J-3 C）。

事故：钧机「line 发送视频超过 25mb 无法发送」——`_media_cap_mb` 在 `limits_mb` 缺省时
一律 25；LINE worker 又独立硬编码 20MB，比路由还低。修法＝`src/inbox/media_limits.py`
单一事实源（内建平台表 LINE 100 / TG 200 / WA 64）+ 路由与 worker 同源消费 +
413 文案带实际大小/差值 + OBS 大文件上传按体积放宽 socket 超时。
"""
from __future__ import annotations

import pytest

from src.inbox import media_limits as ML
from src.integrations import line_media as LM


# ─────────────────── 1. 解析顺序 ───────────────────

def test_builtin_platform_table_when_unconfigured():
    assert ML.platform_media_cap_mb(None, "line") == 100
    assert ML.platform_media_cap_mb({}, "telegram") == 200
    assert ML.platform_media_cap_mb({"inbox": {}}, "whatsapp") == 64
    # 表外平台仍旧行为 25（messenger 网页链路没实测过，不放开）
    assert ML.platform_media_cap_mb(None, "messenger") == ML.DEFAULT_MEDIA_CAP_MB == 25
    assert ML.platform_media_cap_mb(None, "") == 25


def test_explicit_platform_key_wins_over_builtin():
    cfg = {"inbox": {"media": {"limits_mb": {"line": 20}}}}
    assert ML.platform_media_cap_mb(cfg, "line") == 20
    assert ML.platform_media_cap_mb(cfg, "LINE ") == 20  # 大小写/空白归一
    assert ML.platform_media_cap_mb(cfg, "telegram") == 200  # 别的平台不受影响


def test_builtin_beats_default_but_default_covers_unknown():
    """`default` 只影响表外平台——内建表是「更具体的信息」，优先于兜底。"""
    cfg = {"inbox": {"media": {"limits_mb": {"default": 10}}}}
    assert ML.platform_media_cap_mb(cfg, "line") == 100
    assert ML.platform_media_cap_mb(cfg, "messenger") == 10


def test_clamp_and_dirty_values_never_raise():
    assert ML.platform_media_cap_mb({"inbox": {"media": {"limits_mb": {"line": 0}}}}, "line") == 1
    assert ML.platform_media_cap_mb({"inbox": {"media": {"limits_mb": {"line": -5}}}}, "line") == 1
    assert ML.platform_media_cap_mb({"inbox": {"media": {"limits_mb": {"line": 99999}}}}, "line") == 2048
    assert ML.platform_media_cap_mb({"inbox": {"media": {"limits_mb": {"line": "oops"}}}}, "line") == 25
    assert ML.platform_media_cap_mb({"inbox": {"media": {"limits_mb": "garbage"}}}, "line") == 100
    assert ML.platform_media_cap_mb({"inbox": "nope"}, "line") == 100
    assert ML.platform_media_cap_bytes(None, "line") == 100 * ML.MB


def test_describe_over_cap_wording_params():
    d = ML.describe_over_cap(int(31.4 * ML.MB), 25)
    assert d == {"size": 31.4, "cap": 25, "over": 6.4}
    # 大小未知（Content-Length 缺）→ size=0，路由据此回落只报上限的旧文案
    assert ML.describe_over_cap(0, 25) == {"size": 0.0, "cap": 25, "over": 0.0}
    assert ML.describe_over_cap("bad", 25)["size"] == 0.0
    # 恰好不超（近似值误差）→ over 不为负
    assert ML.describe_over_cap(20 * ML.MB, 25)["over"] == 0.0


# ─────────────────── 2. 两个消费方同源 ───────────────────

def test_send_routes_consume_the_same_source():
    from src.web.routes import unified_inbox_send_routes as R
    assert R._media_cap_mb is ML.platform_media_cap_mb
    assert R._describe_over_cap is ML.describe_over_cap


def test_line_worker_outbound_default_follows_media_limits():
    c = LM.resolve_line_media_cfg({"platform_login": {"line": {"media": {}}}})
    assert c["outbound_max_bytes"] == ML.platform_media_cap_bytes(None, "line")
    tight = LM.resolve_line_media_cfg({
        "platform_login": {"line": {"media": {}}},
        "inbox": {"media": {"limits_mb": {"line": 30}}},
    })
    assert tight["outbound_max_bytes"] == 30 * ML.MB
    # worker 自己的显式 outbound_max_bytes 仍最高优先（局部覆写）
    own = LM.resolve_line_media_cfg({
        "platform_login": {"line": {"media": {"outbound_max_bytes": 5 * ML.MB}}},
        "inbox": {"media": {"limits_mb": {"line": 30}}},
    })
    assert own["outbound_max_bytes"] == 5 * ML.MB


def test_i18n_keys_bilingual():
    from src.web.web_i18n import get_translations
    for lang in ("zh", "en"):
        tr = get_translations(lang)
        assert "err.inbox.file_too_large_mb" in tr
        s = tr["err.inbox.file_too_large_detail"]
        for ph in ("{size}", "{cap}", "{over}"):
            assert ph in s, (lang, ph, s)


# ─────────────────── 3. OBS 上传超时按体积放宽 ───────────────────

def test_obs_upload_timeout_scales_with_size_and_is_clamped():
    base, mx = LM.OBS_UPLOAD_TIMEOUT_BASE_SEC, LM.OBS_UPLOAD_TIMEOUT_MAX_SEC
    assert LM.obs_upload_timeout_sec(0) == base
    assert LM.obs_upload_timeout_sec("bad") == base
    t30 = LM.obs_upload_timeout_sec(30 * ML.MB)
    t60 = LM.obs_upload_timeout_sec(60 * ML.MB)
    t100 = LM.obs_upload_timeout_sec(100 * ML.MB)
    assert base < t30 < t60 < t100 <= mx
    # 真机：60MB 在 30s 写超时上撞死 → 放宽后必须远大于 30s
    assert t60 >= 120
    assert LM.obs_upload_timeout_sec(10 ** 12) == mx


class _Cfg:
    def __init__(self, timeout=30.0):
        self.timeout = timeout


class _T:
    def __init__(self, cfg):
        self.config = cfg


class _Obs:
    def __init__(self, cfg):
        self._t = _T(cfg)


class _Api:
    def __init__(self, cfg):
        self.obs = _Obs(cfg)


def test_obs_upload_timeout_ctx_bumps_and_restores():
    cfg = _Cfg(30.0)
    api = _Api(cfg)
    with LM._obs_upload_timeout(api, 60 * ML.MB) as eff:
        assert cfg.timeout == eff == LM.obs_upload_timeout_sec(60 * ML.MB)
        assert cfg.timeout > 30.0
    assert cfg.timeout == 30.0  # 退出恢复，不污染 receiver / 后续请求


def test_obs_upload_timeout_ctx_restores_on_exception():
    cfg = _Cfg(30.0)
    api = _Api(cfg)
    with pytest.raises(RuntimeError):
        with LM._obs_upload_timeout(api, 60 * ML.MB):
            raise RuntimeError("boom")
    assert cfg.timeout == 30.0


def test_obs_upload_timeout_ctx_never_lowers_and_tolerates_unknown_shape():
    # 运营已把 timeout 配得更大 → 不降
    cfg = _Cfg(600.0)
    with LM._obs_upload_timeout(_Api(cfg), 1 * ML.MB) as eff:
        assert cfg.timeout == 600.0 and eff == 600.0
    # 测试假 api / okline 换字段 → 静默不放宽，绝不抛
    with LM._obs_upload_timeout(object(), 60 * ML.MB) as eff:
        assert eff is None
    with LM._obs_upload_timeout(_Api(_Cfg(timeout="weird")), 60 * ML.MB) as eff:
        assert eff == "weird"


# ─────────────────── 4. 编排器 send_media 悬死兜底按体积加时 ───────────────────

def test_orchestrator_send_media_timeout_scales_with_size():
    """45s 悬死兜底不得把正常的 60MB(40s)/100MB(60s) 上传判成 send_timeout。"""
    from src.integrations import account_orchestrator as AO
    base = AO.DEFAULT_SEND_MEDIA_TIMEOUT_SEC
    assert AO._send_media_timeout_sec(None) == base
    assert AO._send_media_timeout_sec(None, 0) == base
    assert AO._send_media_timeout_sec(None, "bad") == base
    # 小文件（图片/语音）几乎不变
    assert AO._send_media_timeout_sec(None, 300 * 1024) < base + 2
    t60 = AO._send_media_timeout_sec(None, 60 * ML.MB)
    t100 = AO._send_media_timeout_sec(None, 100 * ML.MB)
    assert base < t60 < t100 <= AO.SEND_MEDIA_TIMEOUT_CAP_SEC
    assert t60 > 60.5 * 2  # 真机实测 100MB 60.5s；60MB 档也要有充足余量
    assert AO._send_media_timeout_sec(None, 10 ** 12) == AO.SEND_MEDIA_TIMEOUT_CAP_SEC
    # 运营配置仍是基数；显式关闭（<=0）保持旧语义不被体积项复活
    cfg = {"orchestrator": {"send_media_timeout_sec": 10}}
    assert AO._send_media_timeout_sec(cfg, 0) == 10.0
    assert AO._send_media_timeout_sec(cfg, 60 * ML.MB) > 10.0
    assert AO._send_media_timeout_sec({"orchestrator": {"send_media_timeout_sec": 0}},
                                      60 * ML.MB) == 0.0


def test_orchestrator_send_media_passes_file_size_to_timeout():
    import inspect
    from src.integrations import account_orchestrator as AO
    src = inspect.getsource(AO.AccountOrchestrator.send_media)
    assert "_send_media_timeout_sec(self._config, _sz)" in src
    assert "os.path.getsize(_send_path)" in src


def test_send_line_media_upload_goes_through_timeout_ctx(monkeypatch):
    """send_line_media 的 OBS 上传必须包在放宽超时里（静态接线钉住，别被重构掉）。"""
    import inspect
    src = inspect.getsource(LM.send_line_media)
    assert "_obs_upload_timeout(api, len(data))" in src
    assert src.index("_obs_upload_timeout(") < src.index("upload_message_object(")
