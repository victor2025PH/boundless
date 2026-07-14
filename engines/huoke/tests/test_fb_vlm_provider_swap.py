# -*- coding: utf-8 -*-
"""P5b + P5c (2026-04-24) — Gemini 503 peak-hour → Ollama runtime swap。

分 3 组:
  * TestLLMClientLastError — P5c: _call_api 在 retry 循环里保留 last_error
    code + body; 成功 reset; timeout → code=None, body="timeout".
  * TestTryOllamaVisionClient — P5b: probe localhost:11434 有 vision model 才
    返 LLMClient, 否则 None.
  * TestRecordVlmResult — P5b: 连续 N 次 HTTP 失败 → swap Gemini → Ollama;
    non-Gemini / Ollama 不可用 / 已 swap 不再 flip-flop。

所有 httpx 层都 patch 掉 — 不打网络。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest


# ─── P5c · LLMClient.last_error_code / last_error_body ─────────────

class TestLLMClientLastError:
    """_call_api retry 循环里 code+body 的记录语义 (成功清, 失败留最后一次)。"""

    def _client(self, max_retries=2):
        from src.ai.llm_client import LLMClient, LLMConfig
        return LLMClient(LLMConfig(
            provider="gemini", api_key="fake", max_retries=max_retries,
            cache_enabled=False))

    def test_init_defaults_none(self):
        c = self._client()
        assert c.last_error_code is None
        assert c.last_error_body == ""

    def test_success_clears_error_state(self):
        """先人为写脏, 再 200 → clear。"""
        c = self._client()
        c.last_error_code = 500
        c.last_error_body = "old error"
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        with patch.object(c._http, "post", return_value=ok_resp):
            out = c._call_api({"model": "x", "messages": []})
        assert out == "hi"
        assert c.last_error_code is None
        assert c.last_error_body == ""

    def test_503_preserves_code_and_body(self):
        c = self._client(max_retries=2)
        fail_resp = MagicMock()
        fail_resp.status_code = 503
        fail_resp.text = "Service Unavailable: model overloaded"
        with patch.object(c._http, "post", return_value=fail_resp), \
             patch("time.sleep"):  # 跳过 retry backoff
            out = c._call_api({"model": "x", "messages": []})
        assert out == ""
        assert c.last_error_code == 503
        assert "overloaded" in c.last_error_body

    def test_body_truncated_to_500(self):
        c = self._client(max_retries=1)
        fail_resp = MagicMock()
        fail_resp.status_code = 500
        fail_resp.text = "A" * 2000
        with patch.object(c._http, "post", return_value=fail_resp), \
             patch("time.sleep"):
            c._call_api({"model": "x", "messages": []})
        assert len(c.last_error_body) == 500

    def test_timeout_sets_body_timeout_code_none(self):
        c = self._client(max_retries=1)
        with patch.object(
                c._http, "post",
                side_effect=httpx.TimeoutException("slow")), \
             patch("time.sleep"):
            out = c._call_api({"model": "x", "messages": []})
        assert out == ""
        assert c.last_error_code is None
        assert c.last_error_body == "timeout"

    def test_exception_sets_body_code_none(self):
        c = self._client(max_retries=1)
        with patch.object(c._http, "post",
                          side_effect=RuntimeError("net blew")), \
             patch("time.sleep"):
            c._call_api({"model": "x", "messages": []})
        assert c.last_error_code is None
        assert "net blew" in c.last_error_body

    def test_last_error_keeps_final_retry(self):
        """多次 retry 时 last_error_body 应是最后一次的 body (覆盖)。"""
        c = self._client(max_retries=3)
        r1 = MagicMock(status_code=503)
        r1.text = "first"
        r2 = MagicMock(status_code=503)
        r2.text = "second"
        r3 = MagicMock(status_code=503)
        r3.text = "third-final"
        with patch.object(c._http, "post", side_effect=[r1, r2, r3]), \
             patch("time.sleep"):
            c._call_api({"model": "x", "messages": []})
        assert c.last_error_code == 503
        assert c.last_error_body == "third-final"


# ─── P5b · _try_ollama_vision_client ────────────────────────────────

class TestTryOllamaVisionClient:

    def test_ollama_down_returns_none(self):
        from src.app_automation.facebook import _try_ollama_vision_client
        with patch("httpx.get", side_effect=RuntimeError("conn refused")):
            assert _try_ollama_vision_client() is None

    def test_non_200_returns_none(self):
        from src.app_automation.facebook import _try_ollama_vision_client
        fake = MagicMock(status_code=500)
        with patch("httpx.get", return_value=fake):
            assert _try_ollama_vision_client() is None

    def test_no_vision_model_returns_none(self):
        """Ollama 有 model 但没 vision → None (chat-only 不能 VLM)。"""
        from src.app_automation.facebook import _try_ollama_vision_client
        fake = MagicMock(status_code=200)
        fake.json.return_value = {"models": [{"name": "qwen2.5:7b"},
                                              {"name": "llama3.2"}]}
        with patch("httpx.get", return_value=fake):
            assert _try_ollama_vision_client() is None

    def test_llava_available_returns_client(self):
        from src.app_automation.facebook import _try_ollama_vision_client
        fake = MagicMock(status_code=200)
        fake.json.return_value = {"models": [{"name": "llava:7b"},
                                              {"name": "qwen2.5:7b"}]}
        with patch("httpx.get", return_value=fake):
            c = _try_ollama_vision_client()
        assert c is not None
        assert c.config.provider == "ollama"
        assert "llava" in c.config.vision_model

    @pytest.mark.parametrize("model", [
        "moondream:latest", "minicpm-v:8b", "bakllava:7b", "qwen2.5vl:7b"])
    def test_vision_model_matches_various_names(self, model):
        from src.app_automation.facebook import _try_ollama_vision_client
        fake = MagicMock(status_code=200)
        fake.json.return_value = {"models": [{"name": model}]}
        with patch("httpx.get", return_value=fake):
            c = _try_ollama_vision_client()
        assert c is not None
        assert c.config.vision_model == model

    def test_empty_models_list_returns_none(self):
        from src.app_automation.facebook import _try_ollama_vision_client
        fake = MagicMock(status_code=200)
        fake.json.return_value = {"models": []}
        with patch("httpx.get", return_value=fake):
            assert _try_ollama_vision_client() is None

    def test_missing_models_key_returns_none(self):
        from src.app_automation.facebook import _try_ollama_vision_client
        fake = MagicMock(status_code=200)
        fake.json.return_value = {}  # no 'models'
        with patch("httpx.get", return_value=fake):
            assert _try_ollama_vision_client() is None


# ─── P5b · _record_vlm_result (consecutive failure → provider swap) ──

def _fake_vf(provider="gemini", err_code=None, err_body=""):
    """造 "mini VisionFallback" stub - 只需 `_client.config.provider` +
    `_client.last_error_code` / `.last_error_body`。"""
    client = SimpleNamespace(
        config=SimpleNamespace(provider=provider, vision_model="m"),
        last_error_code=err_code,
        last_error_body=err_body,
    )
    return SimpleNamespace(_client=client)


@pytest.fixture(autouse=True)
def _reset_swap_state():
    """每个测试前 reset 模块全局 counter — 避免跨 test 污染。"""
    import src.app_automation.facebook as fb
    fb._vlm_consecutive_failures = 0
    fb._vlm_provider_swapped = False
    fb._vision_fallback_instance = None
    yield
    fb._vlm_consecutive_failures = 0
    fb._vlm_provider_swapped = False
    fb._vision_fallback_instance = None


class TestRecordVlmResult:

    def test_success_resets_counter(self):
        import src.app_automation.facebook as fb
        fb._vlm_consecutive_failures = 2
        vf = _fake_vf(err_code=None, err_body="")
        fb._record_vlm_result(vf)
        assert fb._vlm_consecutive_failures == 0

    def test_http_error_increments(self):
        import src.app_automation.facebook as fb
        vf = _fake_vf(err_code=503, err_body="overloaded")
        fb._record_vlm_result(vf)
        assert fb._vlm_consecutive_failures == 1
        fb._record_vlm_result(vf)
        assert fb._vlm_consecutive_failures == 2

    def test_timeout_counts_as_failure(self):
        import src.app_automation.facebook as fb
        vf = _fake_vf(err_code=None, err_body="timeout")
        fb._record_vlm_result(vf)
        assert fb._vlm_consecutive_failures == 1

    def test_non_http_failure_resets(self):
        """code=None AND body != 'timeout' → 非 HTTP 问题 (parser fail 等),
        不计入 swap 决策 (swap 只对 HTTP 层问题意义)。"""
        import src.app_automation.facebook as fb
        fb._vlm_consecutive_failures = 2
        vf = _fake_vf(err_code=None, err_body="")  # 成功 or 非 HTTP
        fb._record_vlm_result(vf)
        assert fb._vlm_consecutive_failures == 0

    def test_below_threshold_no_swap(self):
        """失败次数 < 3 → 不 swap, counter 累加。"""
        import src.app_automation.facebook as fb
        vf = _fake_vf(err_code=503, err_body="high demand")
        with patch("src.app_automation.facebook._try_ollama_vision_client"
                   ) as m:
            fb._record_vlm_result(vf)
            fb._record_vlm_result(vf)
        assert fb._vlm_consecutive_failures == 2
        assert fb._vlm_provider_swapped is False
        m.assert_not_called()  # 未达阈值就不探 Ollama

    def test_threshold_triggers_swap_when_ollama_available(self):
        import src.app_automation.facebook as fb
        vf = _fake_vf(err_code=503, err_body="high demand")
        fake_ollama = SimpleNamespace(
            config=SimpleNamespace(provider="ollama", vision_model="llava:7b"))
        fake_vf_cls = MagicMock()
        with patch("src.app_automation.facebook._try_ollama_vision_client",
                   return_value=fake_ollama), \
             patch("src.ai.vision_fallback.VisionFallback", fake_vf_cls):
            fb._record_vlm_result(vf)
            fb._record_vlm_result(vf)
            fb._record_vlm_result(vf)  # 第 3 次 → swap
        assert fb._vlm_provider_swapped is True
        assert fb._vlm_consecutive_failures == 0  # reset 后 counter 归零
        fake_vf_cls.assert_called_once_with(client=fake_ollama)
        # 新 instance 已写入 global
        assert fb._vision_fallback_instance is fake_vf_cls.return_value

    def test_threshold_no_ollama_no_swap(self):
        """Ollama 不可用 → 保持 Gemini, 不 swap, 不 reset counter。"""
        import src.app_automation.facebook as fb
        vf = _fake_vf(err_code=503, err_body="high demand")
        with patch("src.app_automation.facebook._try_ollama_vision_client",
                   return_value=None):
            fb._record_vlm_result(vf)
            fb._record_vlm_result(vf)
            fb._record_vlm_result(vf)
        assert fb._vlm_provider_swapped is False
        assert fb._vlm_consecutive_failures == 3  # 继续累加可重试

    def test_non_gemini_provider_no_swap(self):
        """当前 provider 非 Gemini (e.g. 已是 Ollama, 或 OpenAI) → 不 swap。"""
        import src.app_automation.facebook as fb
        vf = _fake_vf(provider="ollama", err_code=500, err_body="down")
        with patch("src.app_automation.facebook._try_ollama_vision_client"
                   ) as m:
            for _ in range(5):
                fb._record_vlm_result(vf)
        assert fb._vlm_provider_swapped is False
        m.assert_not_called()

    def test_already_swapped_no_reswap(self):
        """已 swap 过一次 → 再失败不再触发 (avoid flip-flop)。"""
        import src.app_automation.facebook as fb
        fb._vlm_provider_swapped = True
        vf = _fake_vf(err_code=503, err_body="oops")
        with patch("src.app_automation.facebook._try_ollama_vision_client"
                   ) as m:
            for _ in range(10):
                fb._record_vlm_result(vf)
        m.assert_not_called()
        assert fb._vlm_provider_swapped is True

    def test_vf_none_safe(self):
        import src.app_automation.facebook as fb
        fb._record_vlm_result(None)  # 不崩
        assert fb._vlm_consecutive_failures == 0

    def test_vf_no_client_attr_safe(self):
        import src.app_automation.facebook as fb
        fb._record_vlm_result(SimpleNamespace(_client=None))
        assert fb._vlm_consecutive_failures == 0

    def test_success_after_failures_resets(self):
        """失败 2 次后成功 → counter 归零 (不 leaking 到后续)。"""
        import src.app_automation.facebook as fb
        fail_vf = _fake_vf(err_code=503, err_body="oops")
        ok_vf = _fake_vf(err_code=None, err_body="")
        fb._record_vlm_result(fail_vf)
        fb._record_vlm_result(fail_vf)
        assert fb._vlm_consecutive_failures == 2
        fb._record_vlm_result(ok_vf)
        assert fb._vlm_consecutive_failures == 0
        # 再失败从 0 重新累加
        fb._record_vlm_result(fail_vf)
        assert fb._vlm_consecutive_failures == 1
