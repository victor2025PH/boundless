# -*- coding: utf-8 -*-
"""`scripts/messenger_vlm_prompt_eval.py` — mock flow test.

不打真 LLM. 覆盖:
  * load_cases 解析 yaml (带 bbox tuple 转换)
  * resolve_screenshot 查找路径 (base_dir / screenshots/ / cwd / abs)
  * run_one 5 种 status: HIT / WRONG / MISS / SKIP / ERROR
  * render_text 输出含 summary + hit rate
  * results_to_dicts JSON 序列化
  * main() CLI 参数 + exit code
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def eval_mod():
    """`scripts/` 是 implicit namespace package (同 test_auto_merge_stack pattern)。"""
    from scripts import messenger_vlm_prompt_eval as mod
    return mod


# ─── load_cases ──────────────────────────────────────────────────────

class TestLoadCases:

    def test_load_valid_cases(self, eval_mod, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text(
            "- screenshot: a.png\n"
            "  target: find A\n"
            "  ground_truth_bbox: [1, 2, 3, 4]\n"
            "- screenshot: b.png\n"
            "  target: find B\n"
            "  context: in top\n"
            "  ground_truth_bbox: [10, 20, 30, 40]\n"
            "  note: key case\n",
            encoding="utf-8")
        cases = eval_mod.load_cases(p)
        assert len(cases) == 2
        assert cases[0].screenshot == "a.png"
        assert cases[0].ground_truth_bbox == (1, 2, 3, 4)
        assert cases[1].context == "in top"
        assert cases[1].note == "key case"

    def test_load_empty_yaml(self, eval_mod, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("", encoding="utf-8")
        assert eval_mod.load_cases(p) == []

    def test_load_skips_malformed(self, eval_mod, tmp_path, capsys):
        p = tmp_path / "c.yaml"
        p.write_text(
            "- screenshot: ok.png\n"
            "  target: valid\n"
            "- unknown_field: 1\n"  # missing required
            "  target: bad\n",
            encoding="utf-8")
        cases = eval_mod.load_cases(p)
        assert len(cases) == 1
        assert "skipping" in capsys.readouterr().err.lower()


# ─── resolve_screenshot ──────────────────────────────────────────────

class TestResolveScreenshot:

    def test_found_in_base_dir(self, eval_mod, tmp_path):
        (tmp_path / "img.png").write_bytes(b"fake")
        r = eval_mod.resolve_screenshot("img.png", tmp_path)
        assert r and r.exists()

    def test_found_in_screenshots_subdir(self, eval_mod, tmp_path):
        sub = tmp_path / "screenshots"
        sub.mkdir()
        (sub / "x.png").write_bytes(b"fake")
        r = eval_mod.resolve_screenshot("x.png", tmp_path)
        assert r and r.exists() and r.parent.name == "screenshots"

    def test_not_found_returns_none(self, eval_mod, tmp_path):
        assert eval_mod.resolve_screenshot("missing.png", tmp_path) is None


# ─── run_one 5 种 status ─────────────────────────────────────────────

class TestRunOne:

    def _case(self, eval_mod, **kw):
        defaults = dict(screenshot="x.png", target="search bar",
                        context="top", ground_truth_bbox=(100, 200, 500, 300))
        defaults.update(kw)
        return eval_mod.EvalCase(**defaults)

    def _vf_returning(self, eval_mod, coords=None, raw="", raises=False):
        vf = MagicMock()
        if raises:
            vf.find_element = MagicMock(side_effect=RuntimeError("vlm boom"))
        elif coords is None:
            r = MagicMock()
            r.coordinates = None
            r.raw_response = raw
            vf.find_element = MagicMock(return_value=r)
        else:
            r = MagicMock()
            r.coordinates = coords
            r.raw_response = raw
            vf.find_element = MagicMock(return_value=r)
        return vf

    def test_hit(self, eval_mod, tmp_path):
        (tmp_path / "x.png").write_bytes(b"fake")
        vf = self._vf_returning(eval_mod, coords=(300, 250))
        r = eval_mod.run_one(vf, self._case(eval_mod), tmp_path)
        assert r.status == "HIT"
        assert r.coordinates == (300, 250)

    def test_wrong(self, eval_mod, tmp_path):
        (tmp_path / "x.png").write_bytes(b"fake")
        vf = self._vf_returning(eval_mod, coords=(50, 50))  # out of bbox
        r = eval_mod.run_one(vf, self._case(eval_mod), tmp_path)
        assert r.status == "WRONG"
        assert r.coordinates == (50, 50)

    def test_miss_no_coords(self, eval_mod, tmp_path):
        (tmp_path / "x.png").write_bytes(b"fake")
        vf = self._vf_returning(eval_mod, coords=None, raw="NOT_FOUND")
        r = eval_mod.run_one(vf, self._case(eval_mod), tmp_path)
        assert r.status == "MISS"
        assert "NOT_FOUND" in r.raw_response

    def test_skip_no_screenshot(self, eval_mod, tmp_path):
        # no .png file created
        vf = self._vf_returning(eval_mod, coords=(300, 250))
        r = eval_mod.run_one(vf, self._case(eval_mod), tmp_path)
        assert r.status == "SKIP"
        assert "not found" in r.error
        # VLM shouldn't be called when screenshot missing
        vf.find_element.assert_not_called()

    def test_error_vlm_exception(self, eval_mod, tmp_path):
        (tmp_path / "x.png").write_bytes(b"fake")
        vf = self._vf_returning(eval_mod, raises=True)
        r = eval_mod.run_one(vf, self._case(eval_mod), tmp_path)
        assert r.status == "ERROR"
        assert "vlm boom" in r.error


# ─── render_text ─────────────────────────────────────────────────────

class TestRenderText:

    def test_includes_hit_rate_and_summary(self, eval_mod):
        cases_results = [
            eval_mod.EvalResult(
                case=eval_mod.EvalCase(
                    screenshot="a.png", target="t1",
                    ground_truth_bbox=(0, 0, 100, 100)),
                status="HIT", coordinates=(50, 50), latency_sec=1.2),
            eval_mod.EvalResult(
                case=eval_mod.EvalCase(
                    screenshot="b.png", target="t2",
                    ground_truth_bbox=(0, 0, 100, 100)),
                status="MISS", latency_sec=2.5, raw_response="none"),
        ]
        text = eval_mod.render_text(cases_results)
        assert "HIT" in text
        assert "MISS" in text
        assert "Hit rate: 1/2" in text
        assert "Avg latency:" in text

    def test_empty_results(self, eval_mod):
        text = eval_mod.render_text([])
        assert "Hit rate: 0/0" in text
        assert "total 0" in text


# ─── results_to_dicts JSON ──────────────────────────────────────────

class TestResultsToDicts:

    def test_json_serializable(self, eval_mod):
        r = eval_mod.EvalResult(
            case=eval_mod.EvalCase(
                screenshot="a.png", target="t", context="ctx",
                ground_truth_bbox=(1, 2, 3, 4), note="n"),
            status="HIT", coordinates=(5, 6), latency_sec=1.5,
            error="", raw_response="COORDINATES: 5, 6")
        dicts = eval_mod.results_to_dicts([r])
        # must be JSON-serializable
        s = json.dumps(dicts)
        assert "HIT" in s and "COORDINATES" in s
        # bbox / coordinates 应为 list (JSON-friendly)
        assert dicts[0]["ground_truth_bbox"] == [1, 2, 3, 4] or \
               dicts[0]["ground_truth_bbox"] == (1, 2, 3, 4)
        assert dicts[0]["coordinates"] == [5, 6]


# ─── main CLI ────────────────────────────────────────────────────────

class TestMain:

    def test_missing_cases_file_returns_2(self, eval_mod, tmp_path, capsys):
        rc = eval_mod.main(["--cases", str(tmp_path / "nonexistent.yaml")])
        assert rc == 2
        assert "not found" in capsys.readouterr().err

    def test_no_provider_returns_2(self, eval_mod, tmp_path, capsys):
        cases = tmp_path / "c.yaml"
        cases.write_text("- screenshot: a.png\n  target: t\n",
                         encoding="utf-8")
        with patch("src.ai.llm_client.get_free_vision_client",
                   return_value=None):
            rc = eval_mod.main(["--cases", str(cases)])
        assert rc == 2
        assert "no free VLM provider" in capsys.readouterr().err

    def test_all_hit_returns_0(self, eval_mod, tmp_path, capsys):
        (tmp_path / "a.png").write_bytes(b"fake")
        cases = tmp_path / "c.yaml"
        cases.write_text(
            "- screenshot: a.png\n"
            "  target: t\n"
            "  ground_truth_bbox: [0, 0, 1000, 1000]\n",
            encoding="utf-8")
        fake_client = MagicMock()
        fake_client.config.provider = "gemini"
        fake_client.config.vision_model = "gemini-2.5-flash"
        fake_vf_result = MagicMock()
        fake_vf_result.coordinates = (100, 200)
        fake_vf_result.raw_response = ""
        with patch("src.ai.llm_client.get_free_vision_client",
                   return_value=fake_client), \
             patch("src.ai.vision_fallback.VisionFallback") as mock_vf_cls:
            mock_vf_cls.return_value.find_element = MagicMock(
                return_value=fake_vf_result)
            rc = eval_mod.main(["--cases", str(cases)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "HIT" in out
        assert "Hit rate: 1/1" in out

    def test_miss_returns_1(self, eval_mod, tmp_path):
        (tmp_path / "a.png").write_bytes(b"fake")
        cases = tmp_path / "c.yaml"
        cases.write_text(
            "- screenshot: a.png\n"
            "  target: t\n"
            "  ground_truth_bbox: [0, 0, 10, 10]\n",
            encoding="utf-8")
        fake_client = MagicMock()
        fake_client.config.provider = "gemini"
        fake_client.config.vision_model = "gemini-2.5-flash"
        fake_vf_result = MagicMock()
        fake_vf_result.coordinates = None
        fake_vf_result.raw_response = "NOT_FOUND"
        with patch("src.ai.llm_client.get_free_vision_client",
                   return_value=fake_client), \
             patch("src.ai.vision_fallback.VisionFallback") as mock_vf_cls:
            mock_vf_cls.return_value.find_element = MagicMock(
                return_value=fake_vf_result)
            rc = eval_mod.main(["--cases", str(cases)])
        assert rc == 1

    def test_json_output_structure(self, eval_mod, tmp_path, capsys):
        (tmp_path / "a.png").write_bytes(b"fake")
        cases = tmp_path / "c.yaml"
        cases.write_text(
            "- screenshot: a.png\n"
            "  target: t\n"
            "  ground_truth_bbox: [0, 0, 1000, 1000]\n",
            encoding="utf-8")
        fake_client = MagicMock()
        fake_client.config.provider = "gemini"
        fake_client.config.vision_model = "gemini-2.5-flash"
        fake_vf_result = MagicMock()
        fake_vf_result.coordinates = (100, 200)
        fake_vf_result.raw_response = ""
        with patch("src.ai.llm_client.get_free_vision_client",
                   return_value=fake_client), \
             patch("src.ai.vision_fallback.VisionFallback") as mock_vf_cls:
            mock_vf_cls.return_value.find_element = MagicMock(
                return_value=fake_vf_result)
            eval_mod.main(["--cases", str(cases), "--json"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "results" in data
        assert "provider" in data
        assert data["provider"] == "gemini"
        assert data["results"][0]["status"] == "HIT"


# ─── P7 (2026-04-24): --throttle-sec / --progress ───────────────────

def _cases_file(tmp_path, n=3):
    """Write n cases + n .png stubs, return path to cases yaml."""
    lines = []
    for i in range(n):
        (tmp_path / f"case{i}.png").write_bytes(b"fake")
        lines.append(f"- screenshot: case{i}.png")
        lines.append(f"  target: t{i}")
        lines.append(f"  ground_truth_bbox: [0, 0, 1000, 1000]")
    cases = tmp_path / "c.yaml"
    cases.write_text("\n".join(lines), encoding="utf-8")
    return cases


def _mock_vlm(coords=(100, 200)):
    """Fake vision fallback result fixture."""
    r = MagicMock()
    r.coordinates = coords
    r.raw_response = ""
    return r


class TestThrottle:
    """--throttle-sec 2026-04-24: 背靠背 Gemini 打爆 free tier RPM, 加 sleep
    让 eval 真反映 prompt 质量而非 429 backoff。"""

    def test_default_no_sleep(self, eval_mod, tmp_path):
        """无 --throttle-sec → 不 sleep (向后兼容)。"""
        cases = _cases_file(tmp_path, n=3)
        fake_client = MagicMock()
        fake_client.config.provider = "gemini"
        fake_client.config.vision_model = "gemini-2.5-flash"
        fake_vf = MagicMock()
        fake_vf.find_element = MagicMock(return_value=_mock_vlm())
        with patch("src.ai.llm_client.get_free_vision_client",
                   return_value=fake_client), \
             patch("src.ai.vision_fallback.VisionFallback",
                   return_value=fake_vf), \
             patch(f"{eval_mod.__name__}.time.sleep") as sleep:
            rc = eval_mod.main(["--cases", str(cases)])
        assert rc == 0
        sleep.assert_not_called()

    def test_throttle_sleeps_between_not_before_or_after(self, eval_mod,
                                                          tmp_path):
        """--throttle-sec 5 + 4 cases → 3 次 sleep(5), 不在首 case 前或末 case 后。"""
        cases = _cases_file(tmp_path, n=4)
        fake_client = MagicMock()
        fake_client.config.provider = "gemini"
        fake_client.config.vision_model = "gemini-2.5-flash"
        fake_vf = MagicMock()
        fake_vf.find_element = MagicMock(return_value=_mock_vlm())
        with patch("src.ai.llm_client.get_free_vision_client",
                   return_value=fake_client), \
             patch("src.ai.vision_fallback.VisionFallback",
                   return_value=fake_vf), \
             patch(f"{eval_mod.__name__}.time.sleep") as sleep:
            eval_mod.main(["--cases", str(cases), "--throttle-sec", "5"])
        # 4 cases → 3 gaps (between cases). 首 case 前不 sleep, 末 case 后也不。
        assert sleep.call_count == 3
        # 每次 sleep 都是 5s
        for c in sleep.call_args_list:
            assert c.args[0] == 5.0

    def test_throttle_single_case_no_sleep(self, eval_mod, tmp_path):
        """只有 1 case → 不 sleep (无 gap)。"""
        cases = _cases_file(tmp_path, n=1)
        fake_client = MagicMock()
        fake_client.config.provider = "gemini"
        fake_client.config.vision_model = "gemini-2.5-flash"
        fake_vf = MagicMock()
        fake_vf.find_element = MagicMock(return_value=_mock_vlm())
        with patch("src.ai.llm_client.get_free_vision_client",
                   return_value=fake_client), \
             patch("src.ai.vision_fallback.VisionFallback",
                   return_value=fake_vf), \
             patch(f"{eval_mod.__name__}.time.sleep") as sleep:
            eval_mod.main(["--cases", str(cases), "--throttle-sec", "30"])
        sleep.assert_not_called()

    def test_throttle_zero_no_sleep(self, eval_mod, tmp_path):
        """显式 --throttle-sec 0 也不 sleep。"""
        cases = _cases_file(tmp_path, n=3)
        fake_client = MagicMock()
        fake_client.config.provider = "gemini"
        fake_client.config.vision_model = "gemini-2.5-flash"
        fake_vf = MagicMock()
        fake_vf.find_element = MagicMock(return_value=_mock_vlm())
        with patch("src.ai.llm_client.get_free_vision_client",
                   return_value=fake_client), \
             patch("src.ai.vision_fallback.VisionFallback",
                   return_value=fake_vf), \
             patch(f"{eval_mod.__name__}.time.sleep") as sleep:
            eval_mod.main(["--cases", str(cases), "--throttle-sec", "0"])
        sleep.assert_not_called()


class TestProgress:
    """--progress 让长跑能被观察 (back-to-back 14 cases throttled 7min)。"""

    def test_progress_writes_stderr_per_case(self, eval_mod, tmp_path, capsys):
        cases = _cases_file(tmp_path, n=2)
        fake_client = MagicMock()
        fake_client.config.provider = "gemini"
        fake_client.config.vision_model = "gemini-2.5-flash"
        fake_vf = MagicMock()
        fake_vf.find_element = MagicMock(return_value=_mock_vlm())
        with patch("src.ai.llm_client.get_free_vision_client",
                   return_value=fake_client), \
             patch("src.ai.vision_fallback.VisionFallback",
                   return_value=fake_vf):
            eval_mod.main(["--cases", str(cases), "--progress"])
        err = capsys.readouterr().err
        # 2 cases → 2 progress 行
        assert err.count("[ 1/2]") == 1
        assert err.count("[ 2/2]") == 1
        assert "HIT" in err

    def test_no_progress_silent_stderr(self, eval_mod, tmp_path, capsys):
        cases = _cases_file(tmp_path, n=2)
        fake_client = MagicMock()
        fake_client.config.provider = "gemini"
        fake_client.config.vision_model = "gemini-2.5-flash"
        fake_vf = MagicMock()
        fake_vf.find_element = MagicMock(return_value=_mock_vlm())
        with patch("src.ai.llm_client.get_free_vision_client",
                   return_value=fake_client), \
             patch("src.ai.vision_fallback.VisionFallback",
                   return_value=fake_vf):
            eval_mod.main(["--cases", str(cases)])
        err = capsys.readouterr().err
        # 无 progress 应 stderr 干净 (不含 case 行)
        assert "[ 1/2]" not in err
        assert "[ 2/2]" not in err
