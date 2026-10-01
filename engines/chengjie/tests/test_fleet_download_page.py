"""Public download pages say the same thing (P0-4): /downloads/fleet/ (generated) and /fleet/ (fleet_home.html).

- One primary button: the installer. The single-file exe is a small link for upgrades / technicians.
- Same install story on both pages: no code is typed during install; the finish page shows a
  pairing code that the admin approves; room packs (Install.cmd) are optional and admin-issued.
- SmartScreen guidance: 「更多信息」 then 「仍要运行」.
- Product name 智拓群控. Version / size / SHA-256 come from the published files.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

ENGINE = Path(__file__).resolve().parents[1]
GEN = ENGINE / "deploy" / "fleet" / "render_download_page.py"
SHA_A = "8fdd8cc2b7d868bf8bbce01a34abdd6eb760452046bcd7cc07fb3876acb9d275"
SHA_B = "d0012b875c848d013eba3ad7d9dc580c689546c47aba4f3a36f5c398b58cb28e"

UNIFIED = "不需要输入任何安装码或注册码"


def _gen():
    spec = importlib.util.spec_from_file_location("render_download_page", GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _home(**download):
    env = Environment(loader=FileSystemLoader(str(ENGINE / "domains/fleet_control/web/templates")),
                      autoescape=select_autoescape(["html"]))
    return env.get_template("fleet_home.html").render(
        download=download, public_url="https://bd2026.cc/fleet", proto_version=1)


def test_generated_page_has_version_size_sha_and_one_primary_button():
    g = _gen()
    page = g.render("0.3.3", {"sha256": SHA_A, "size": 20773182}, {"sha256": SHA_B, "size": 19570602},
                    date="2026-10-01")
    assert '<meta name="fleet-download-version" content="0.3.3">' in page
    assert "/downloads/fleet/ChatXAgentSetup-0.3.3.exe" in page and "19.8 MB" in page and SHA_A in page
    assert "/downloads/fleet/chatx-agent-0.3.3.exe" in page and "18.7 MB" in page and SHA_B in page
    assert page.count('class="btn"') == 1
    assert page.index("ChatXAgentSetup-0.3.3.exe") < page.index("chatx-agent-0.3.3.exe")
    assert "供升级或技术人员使用" in page
    assert "智拓群控" in page and "ChatX 群控 Agent" not in page
    assert "「更多信息」" in page and "「仍要运行」" in page
    assert UNIFIED in page and "配对码" in page and "Install.cmd" in page
    assert "按提示填入" not in page and "以管理员身份运行" not in page


def test_generated_page_without_agent_and_bad_input():
    g = _gen()
    page = g.render("0.4.0", {"sha256": SHA_A, "size": 1048576})
    assert "chatx-agent-" not in page and "1.0 MB" in page
    with pytest.raises(ValueError):
        g.render("0.3.3;rm", {"sha256": SHA_A, "size": 1})
    with pytest.raises(ValueError):
        g.render("0.3.3", {"sha256": "nothex", "size": 1})


def test_generator_cli_hashes_real_files_and_writes_utf8_without_bom(tmp_path):
    g = _gen()
    setup = tmp_path / "ChatXAgentSetup.exe"
    agent = tmp_path / "chatx-agent.exe"
    setup.write_bytes(b"MZ setup" * 1000)
    agent.write_bytes(b"MZ agent" * 500)
    out = tmp_path / "index.html"
    assert g.main(["--version", "1.2.3", "--setup", str(setup), "--agent", str(agent),
                   "--date", "2026-10-02", "--out", str(out)]) == 0
    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw
    text = raw.decode("utf-8")
    assert hashlib.sha256(setup.read_bytes()).hexdigest() in text
    assert hashlib.sha256(agent.read_bytes()).hexdigest() in text
    assert "v1.2.3（2026-10-02）" in text


def test_fleet_home_matches_download_page_story():
    html = _home(setup_url="https://bd2026.cc/downloads/fleet/ChatXAgentSetup.exe", version="0.3.3",
                 installer_url="https://bd2026.cc/downloads/fleet/chatx-agent.exe")
    assert UNIFIED in html and "配对码" in html and "机房安装包" in html
    assert "「更多信息」" in html and "「仍要运行」" in html
    assert html.count('class="btn p"') == 1
    assert "单文件版 chatx-agent.exe（供升级或技术人员使用）" in html
    assert "智拓群控" in html and "智控" not in html.replace("智拓群控", "")
    assert "不用输入注册码" not in html and "安装码由管理员发放" not in html
    assert "docs/FLEET_CONTROL_CONTRACT.md" not in html
