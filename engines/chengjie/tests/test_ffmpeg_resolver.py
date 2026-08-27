"""ffmpeg 路径解析器（实施49 P1-11）门禁。

优先级契约：env 覆写 → 冻结布局相对探测（resources/backend/exe 的邻居
resources/ffmpeg/）→ PATH。非冻结不做相对探测（内部部署行为零变化）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.utils import ffmpeg_resolver as fr  # noqa: E402


def test_env_override_wins(tmp_path, monkeypatch):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"x")
    monkeypatch.setenv("CHATX_FFMPEG", str(fake))
    assert fr.ffmpeg_path() == str(fake)


def test_env_missing_file_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATX_FFMPEG", str(tmp_path / "nope.exe"))
    monkeypatch.setattr(fr.shutil, "which", lambda name: None)
    monkeypatch.setattr(fr.sys, "frozen", False, raising=False)
    assert fr.ffmpeg_path() is None


def test_frozen_layout_probing(tmp_path, monkeypatch):
    # resources/backend/app.exe + resources/ffmpeg/ffmpeg.exe
    backend = tmp_path / "resources" / "backend"
    ffdir = tmp_path / "resources" / "ffmpeg"
    backend.mkdir(parents=True)
    ffdir.mkdir(parents=True)
    exe_name = "ffmpeg.exe" if fr.os.name == "nt" else "ffmpeg"
    (ffdir / exe_name).write_bytes(b"x")
    monkeypatch.delenv("CHATX_FFMPEG", raising=False)
    monkeypatch.setattr(fr.sys, "frozen", True, raising=False)
    monkeypatch.setattr(fr.sys, "executable", str(backend / "app.exe"))
    monkeypatch.setattr(fr.shutil, "which", lambda name: None)
    got = fr.ffmpeg_path()
    assert got and Path(got) == ffdir / exe_name


def test_unfrozen_falls_to_path(monkeypatch):
    monkeypatch.delenv("CHATX_FFMPEG", raising=False)
    monkeypatch.setattr(fr.sys, "frozen", False, raising=False)
    monkeypatch.setattr(fr.shutil, "which",
                        lambda name: "/usr/bin/" + name)
    assert fr.ffmpeg_path() == "/usr/bin/ffmpeg"
    assert fr.ffprobe_path() == "/usr/bin/ffprobe"


def test_voice_sender_uses_resolver():
    src = (_ROOT / "src" / "client" / "voice_sender.py").read_text(
        encoding="utf-8")
    assert "from src.utils.ffmpeg_resolver import" in src
    # 转换命令不得再硬编码裸 "ffmpeg"（包内解析会失效）
    import re
    assert not re.search(r'\n\s+"ffmpeg", "-y",', src), (
        "convert_to_ogg_opus 退回了裸 'ffmpeg' 命令名——打包客户机将再次找不到")


def test_packaging_declares_ffmpeg():
    pkg = (_ROOT / "desktop" / "package.json").read_text(encoding="utf-8")
    assert '"from": "build/ffmpeg"' in pkg and '"to": "ffmpeg"' in pkg
    ap = (_ROOT / "desktop" / "build" / "after-pack.js").read_text(
        encoding="utf-8")
    assert "ffmpeg.exe" in ap and "ffprobe.exe" in ap
