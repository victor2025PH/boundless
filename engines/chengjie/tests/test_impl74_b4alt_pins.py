# -*- coding: utf-8 -*-
"""实施74 b4alt 门禁：B76 打包态主日志兜底 + B96/B106 说明文案（B71 金标另在
test_wellbeing_guard）。

- B76（EC4UPG/KVRX2Y 实锤）：桌面部署 logging.file 常为空 → 引擎无 FileHandler →
  runtime_log_files 空手 → 诊断包无主日志。兜底＝收壳捕获的
  ``<userData>/logs/backend.log``（AITR_DATA_DIR=<userData>/data 布局契约）。
- B96（_467）：复制视角链接的说明/成功提示必须写清用途+「首开需登录属正常」。
- B106（_514）：备货页台词库行常驻用途说明（只提速语音，不教 AI 说话）。
"""
from __future__ import annotations

from pathlib import Path

from src.utils.diag_upload import shell_backend_log_files

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


# ── B76 ──────────────────────────────────────────────────────────────────────

def _mk_layout(tmp_path):
    """<userData>/data + <userData>/logs/backend.log 的打包态布局。"""
    user_data = tmp_path / "userData"
    data_dir = user_data / "data"
    data_dir.mkdir(parents=True)
    logs = user_data / "logs"
    logs.mkdir()
    (logs / "backend.log").write_text("boot ok\n", encoding="utf-8")
    return data_dir


def test_shell_backend_log_collected_in_desktop_mode(tmp_path, monkeypatch):
    data_dir = _mk_layout(tmp_path)
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.setenv("AITR_DATA_DIR", str(data_dir))
    got = shell_backend_log_files()
    assert list(got) == ["logs/app/backend.log"]
    assert got["logs/app/backend.log"].is_file()


def test_shell_backend_log_skipped_outside_desktop(tmp_path, monkeypatch):
    """服务器/源码态（无 AITR_DESKTOP_MODE）绝不乱猜路径。"""
    data_dir = _mk_layout(tmp_path)
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    monkeypatch.setenv("AITR_DATA_DIR", str(data_dir))
    assert shell_backend_log_files() == {}


def test_shell_backend_log_missing_file_returns_empty(tmp_path, monkeypatch):
    data_dir = tmp_path / "userData" / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.setenv("AITR_DATA_DIR", str(data_dir))
    assert shell_backend_log_files() == {}


def test_shell_backend_log_no_data_dir_returns_empty(monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.delenv("AITR_DATA_DIR", raising=False)
    assert shell_backend_log_files() == {}


def test_upload_path_wires_shell_collector():
    src = (_ENGINE_ROOT / "src" / "utils" / "diag_upload.py").read_text(
        encoding="utf-8")
    assert "_extra.update(shell_backend_log_files())" in src


# ── B96 / B106 ───────────────────────────────────────────────────────────────

def test_copy_link_copy_explains_login():
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    assert "需登录" in ZH["inbox.acct.copy_link_t"]
    assert "需登录" in ZH["inbox.acct.link_copied"]
    assert "sign" in EN["inbox.acct.copy_link_t"].lower()
    assert "sign" in EN["inbox.acct.link_copied"].lower()


def test_lines_note_bilingual_and_rendered():
    from src.web.i18n_packs.persona_studio import EN, ZH
    assert "不会教 AI" in ZH["psn_stock_lines_note"]
    assert "does not teach" in EN["psn_stock_lines_note"].lower()
    tpl = (_ENGINE_ROOT / "src" / "web" / "templates" / "personas.html"
           ).read_text(encoding="utf-8")
    assert "psn_stock_lines_note" in tpl
