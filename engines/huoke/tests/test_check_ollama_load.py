# -*- coding: utf-8 -*-
"""scripts/check_ollama_load.py 单测 (无真 ollama 依赖)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 让 scripts 可被 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestSummarize:
    def test_empty(self):
        from scripts.check_ollama_load import summarize
        s = summarize({"models": []})
        assert s["count"] == 0
        assert s["total_vram_gb"] == 0.0
        assert s["models"] == []

    def test_single_model(self):
        from scripts.check_ollama_load import summarize
        # 7B model 大约 4.5 GB VRAM
        bytes_4_5gb = int(4.5 * 1024 ** 3)
        s = summarize({"models": [{
            "name": "qwen2.5vl:7b",
            "size_vram": bytes_4_5gb,
            "expires_at": "2026-04-24T15:30:00Z",
            "details": {"parent_model": ""},
        }]})
        assert s["count"] == 1
        assert s["total_vram_gb"] == 4.5
        assert s["models"][0]["name"] == "qwen2.5vl:7b"
        assert s["models"][0]["vram_gb"] == 4.5

    def test_multiple_models_sums_vram(self):
        from scripts.check_ollama_load import summarize
        s = summarize({"models": [
            {"name": "qwen2.5vl:7b",
             "size_vram": int(4.5 * 1024 ** 3),
             "expires_at": "x"},
            {"name": "llava:13b",
             "size_vram": int(7.0 * 1024 ** 3),
             "expires_at": "y"},
        ]})
        assert s["count"] == 2
        assert s["total_vram_gb"] == 11.5

    def test_missing_size_vram_treated_as_zero(self):
        from scripts.check_ollama_load import summarize
        s = summarize({"models": [{"name": "x"}]})
        assert s["models"][0]["vram_gb"] == 0.0


class TestRenderHuman:
    def test_no_models(self):
        from scripts.check_ollama_load import render_human, summarize
        out = render_human(summarize({"models": []}), "http://h")
        assert "0 model loaded (cold)" in out

    def test_with_model(self):
        from scripts.check_ollama_load import render_human, summarize
        s = summarize({"models": [{
            "name": "qwen2.5vl:7b",
            "size_vram": int(4.5 * 1024 ** 3),
            "expires_at": "x",
        }]})
        out = render_human(s, "http://h")
        assert "qwen2.5vl:7b" in out
        assert "4.50" in out
        assert "1 model(s) loaded" in out


class TestMain:
    def test_unreachable_returns_1(self, capsys):
        from scripts.check_ollama_load import main
        with patch("scripts.check_ollama_load.fetch_loaded",
                   return_value=(False, "Connection refused")):
            rc = main([])
        assert rc == 1
        captured = capsys.readouterr()
        assert "unreachable" in captured.err

    def test_reachable_no_models_returns_0(self, capsys):
        from scripts.check_ollama_load import main
        with patch("scripts.check_ollama_load.fetch_loaded",
                   return_value=(True, {"models": []})):
            rc = main([])
        assert rc == 0
        captured = capsys.readouterr()
        assert "0 model loaded" in captured.out

    def test_reachable_with_models_returns_0(self, capsys):
        from scripts.check_ollama_load import main
        with patch("scripts.check_ollama_load.fetch_loaded",
                   return_value=(True, {"models": [{
                       "name": "qwen2.5vl:7b",
                       "size_vram": int(4.5 * 1024 ** 3),
                       "expires_at": "x",
                   }]})):
            rc = main([])
        assert rc == 0
        captured = capsys.readouterr()
        assert "qwen2.5vl:7b" in captured.out

    def test_max_vram_gb_threshold_exit_2(self, capsys):
        from scripts.check_ollama_load import main
        with patch("scripts.check_ollama_load.fetch_loaded",
                   return_value=(True, {"models": [{
                       "name": "llava:13b",
                       "size_vram": int(8.5 * 1024 ** 3),  # > 8 GB threshold
                       "expires_at": "x",
                   }]})):
            rc = main(["--max-vram-gb", "8"])
        assert rc == 2
        captured = capsys.readouterr()
        assert ">" in captured.err and "exit 2" in captured.err

    def test_quiet_no_model_silent(self, capsys):
        from scripts.check_ollama_load import main
        with patch("scripts.check_ollama_load.fetch_loaded",
                   return_value=(True, {"models": []})):
            rc = main(["--quiet"])
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == ""  # quiet mode + no models → no stdout

    def test_json_output(self, capsys):
        from scripts.check_ollama_load import main
        import json as _j
        with patch("scripts.check_ollama_load.fetch_loaded",
                   return_value=(True, {"models": [{
                       "name": "qwen2.5vl:7b",
                       "size_vram": int(4.5 * 1024 ** 3),
                       "expires_at": "x",
                   }]})):
            rc = main(["--json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = _j.loads(captured.out)
        assert data["host"] == "http://127.0.0.1:11434"
        assert data["count"] == 1
        assert data["total_vram_gb"] == 4.5
