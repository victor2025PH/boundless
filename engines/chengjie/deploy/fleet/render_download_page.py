#!/usr/bin/env python3
"""render_download_page.py -- generate the public download page /downloads/fleet/index.html.

The page lives on the VPS at /var/www/dl-mirror/downloads/fleet/index.html (nginx snippet
fleet-downloads.conf). Version, sizes and SHA-256 are computed from the files that are
actually published, so the page can never drift from the binaries. publish_agent.ps1
calls this after copying the versioned files into the mirror.

    python deploy/fleet/render_download_page.py --version 0.3.3 \
        --setup fleet_agent/dist/ChatXAgentSetup.exe --agent fleet_agent/dist/chatx-agent.exe \
        --out index.html

Links always point at the versioned names (ChatXAgentSetup-<ver>.exe, chatx-agent-<ver>.exe):
nginx serves those straight from the mirror, so the hash on the page matches the download.
Release notes come from CHANGELOG.md next to this script.
Stdlib only; runs on Windows (publish machine) and on the VPS (python3). No external CDN.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import html
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

PRODUCT = "智拓群控节点"
TAGLINE = "让电脑接入智拓群控，可远程查看在线状态、升级、清点手机。"
VERSION_RE = re.compile(r"^[0-9][0-9.]*$")  # same shape the nginx location accepts
_CHANGELOG_HEAD = re.compile(r"^##\s+([0-9][0-9.]*)\s*$")
BASE = "/downloads/fleet"
# Used when CHANGELOG.md is missing. Keep the public notes in the markdown file.
DEFAULT_CHANGELOG: Tuple[Tuple[str, str], ...] = (
    ("0.3.6", "上报手机清单。"),
    ("0.3.5", "解决克隆电脑机器码重复。"),
    ("0.3.4", "节点安装器可以把电脑登记到智拓群控，等待管理员批准后上线。"),
)
FEATURES: Tuple[str, ...] = (
    "远程查看这台电脑是否在线。",
    "在智拓群控里升级这台电脑上的节点。",
    "清点这台电脑已连接的手机。",
)


def file_info(path: Path) -> Dict[str, object]:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"sha256": h.hexdigest(), "size": path.stat().st_size}


def human_size(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


def setup_name(version: str) -> str:
    return f"ChatXAgentSetup-{version}.exe"


def agent_name(version: str) -> str:
    return f"chatx-agent-{version}.exe"


def load_changelog(path: Optional[Path] = None) -> List[Tuple[str, str]]:
    """Read ``## <version>`` blocks. A missing or empty file falls back to the built-in notes."""
    src = path if path is not None else Path(__file__).with_name("CHANGELOG.md")
    try:
        text = src.read_text(encoding="utf-8")
    except OSError:
        return list(DEFAULT_CHANGELOG)
    entries: List[Tuple[str, str]] = []
    version = ""
    buf: List[str] = []

    def flush() -> None:
        if not version:
            return
        body = " ".join(part.strip() for part in buf if part.strip())
        if body:
            entries.append((version, body))

    for line in text.splitlines():
        matched = _CHANGELOG_HEAD.match(line.strip())
        if matched:
            flush()
            version = matched.group(1)
            buf = []
        elif version:
            buf.append(line)
    flush()
    return entries or list(DEFAULT_CHANGELOG)


def _sha_row(digest: str) -> str:
    shown = html.escape(digest)
    return (
        '<div class="hashrow"><span class="small">SHA-256</span>'
        f'<code>{shown}</code>'
        f'<button type="button" class="copy" data-sha="{shown}">复制 SHA-256</button></div>'
    )


def _changelog_html(version: str, entries: Sequence[Tuple[str, str]]) -> str:
    items = []
    for ver, text in entries:
        badge = '<span class="cur">当前</span>' if ver == version else ""
        items.append(
            f'<li><span class="v">{html.escape(ver)}</span>{badge} {html.escape(text)}</li>'
        )
    return (
        "<h2>版本历史与更新日志</h2>\n<ul class=\"log\">\n"
        + "\n".join(items)
        + "\n</ul>\n"
    )


_PAGE_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  max-width: 720px; margin: 28px auto; padding: 0 16px 40px; color: #1f2937;
  line-height: 1.65; background: #f8fafc; }
h1 { font-size: 26px; margin: 0 0 6px; line-height: 1.3; }
h2 { font-size: 18px; margin: 26px 0 8px; }
.sub { margin: 0 0 8px; font-size: 16px; }
.ver { color: #6b7280; margin: 0; }
.card, .log li { background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; }
.card { padding: 18px; margin: 16px 0; }
a.btn { display: inline-block; max-width: 100%; background: #2563eb; color: #fff;
  padding: 12px 18px; border-radius: 8px; text-decoration: none; font-weight: 600;
  font-size: 17px; white-space: normal; overflow-wrap: anywhere; text-align: center; }
.hashrow { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-start; margin-top: 12px; }
code { font-size: 12px; word-break: break-all; color: #4b5563; flex: 1 1 auto; min-width: 0; }
button.copy { font: inherit; font-size: 13px; border: 1px solid #cbd5e1; background: #fff;
  border-radius: 8px; padding: 8px 12px; cursor: pointer; }
ul.feats, ol { padding-left: 1.25rem; margin: 8px 0; }
ul.feats li, ol li { margin: 8px 0; }
.small { font-size: 13px; color: #6b7280; }
.tip { border-left: 3px solid #f59e0b; background: #fffbeb; padding: 10px 14px;
  border-radius: 0 8px 8px 0; }
.log { list-style: none; padding: 0; margin: 0; }
.log li { padding: 10px 14px; margin: 8px 0; }
.log .v { font-weight: 700; margin-right: 8px; }
.log .cur { color: #1d4ed8; font-size: 12px; margin-right: 8px; }
@media (max-width: 420px) {
  body { margin: 8px auto; padding: 0 14px 28px; }
  h1 { font-size: 22px; }
  h2 { font-size: 17px; }
  a.btn { display: block; width: 100%; }
  .card { padding: 14px; }
  .hashrow { flex-direction: column; align-items: stretch; }
  code { flex: none; width: 100%; }
  button.copy { width: 100%; }
}
@media (min-width: 1366px) {
  body { max-width: 880px; margin: 48px auto; font-size: 17px; }
  h1 { font-size: 34px; }
  .card { padding: 22px 24px; }
}
"""

_PAGE_JS = """
document.querySelectorAll("button.copy").forEach(function (btn) {
  btn.addEventListener("click", function () {
    var text = btn.getAttribute("data-sha") || "";
    var done = function () { btn.textContent = "已复制"; };
    var fallback = function () {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "readonly");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      try { document.execCommand("copy"); done(); } catch (err) { btn.textContent = "请手动选择复制"; }
      document.body.removeChild(area);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  });
});
"""


def render(version: str, setup: Dict[str, object], agent: Optional[Dict[str, object]] = None,
           date: str = "", base: str = BASE,
           changelog: Optional[Sequence[Tuple[str, str]]] = None) -> str:
    if not VERSION_RE.match(version or ""):
        raise ValueError(f"bad version: {version!r}")
    for info in (setup, agent):
        if info is not None and not re.fullmatch(r"[0-9a-f]{64}", str(info.get("sha256", ""))):
            raise ValueError("sha256 must be 64 lowercase hex chars")
    e = html.escape
    v = e(version)
    setup_href = f"{base}/{setup_name(version)}"
    date_txt = f"（{e(date)}）" if date else ""
    notes = list(changelog) if changelog is not None else load_changelog()
    feats = "\n".join(f"<li>{e(item)}</li>" for item in FEATURES[:3])
    agent_html = ""
    if agent is not None:
        agent_html = (
            f'<p class="small">单文件版（供升级或技术人员使用）：'
            f'<a href="{base}/{agent_name(version)}">{e(agent_name(version))}</a>'
            f'（{e(human_size(int(agent["size"])))}）</p>\n'
            f'{_sha_row(str(agent["sha256"]))}\n'
        )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<!-- generated by deploy/fleet/render_download_page.py; do not edit by hand -->\n'
        f'<meta name="fleet-download-version" content="{v}">\n'
        f'<title>{PRODUCT} · 下载</title>\n<style>{_PAGE_CSS}</style></head><body>\n'
        f'<h1>{PRODUCT}</h1>\n<p class="sub">{e(TAGLINE)}</p>\n'
        f'<p class="ver">当前版本 v{v}{date_txt} · Windows 10 / 11 / Server 2019+（64 位）</p>\n'
        '<div class="card"><p><a class="btn" href="' + e(setup_href) + '">下载安装器（'
        + e(setup_name(version)) + '，' + e(human_size(int(setup["size"]))) + '）</a></p>\n'
        + _sha_row(str(setup["sha256"])) + '</div>\n'
        '<h2>能做什么</h2>\n<ul class="feats">\n' + feats + '\n</ul>\n'
        '<h2>安装步骤</h2>\n<ol>\n'
        '<li>下载本页的安装器。</li>\n'
        '<li>用管理员权限运行安装器。Windows 询问「是否允许此应用对你的设备进行更改」时点「是」。'
        '安装过程不需要输入任何安装码或注册码。</li>\n'
        '<li>安装完成页会显示配对码。打开智拓的「待批准电脑」页，核对同一个配对码后批准，这台电脑就接入了。</li>\n'
        '</ol>\n'
        '<p class="small">整间机房批量安装时，管理员会另外发一个「机房安装包」。打开后双击其中的 Install.cmd，'
        '电脑会直接进入指定分组，不用逐台批准。机房安装包只由管理员私下发放，不在公开页面提供。</p>\n'
        '<h2>被 Windows 拦截时</h2>\n'
        '<p class="tip">安装器尚未做代码签名，所以可能出现蓝色窗口「Windows 已保护你的电脑」。'
        '这不表示文件已损坏。请先点<strong>「更多信息」</strong>，再点<strong>「仍要运行」</strong>'
        '（也就是「更多信息 → 仍要运行」）。介意的话，先点上面的「复制 SHA-256」，和安装包核对一致后再继续。</p>\n'
        + _changelog_html(version, notes)
        + agent_html
        + '<p class="small">产品介绍、重装与安全说明：<a href="/fleet/">bd2026.cc/fleet</a></p>\n'
        f'<script>{_PAGE_JS}</script>\n'
        '</body></html>\n'
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=f"render {PRODUCT} download page")
    ap.add_argument("--version", required=True)
    ap.add_argument("--setup", required=True, type=Path, help="installer exe whose hash/size go on the page")
    ap.add_argument("--agent", type=Path, help="single-file chatx-agent exe (optional small link)")
    ap.add_argument("--date", default="", help="release date shown on the page (default: today)")
    ap.add_argument("--changelog", type=Path, help="markdown release notes (default: CHANGELOG.md beside this script)")
    ap.add_argument("--out", type=Path, help="write here (default: stdout)")
    a = ap.parse_args(argv)
    date = a.date or _dt.date.today().isoformat()
    notes = load_changelog(a.changelog) if a.changelog else None
    page = render(a.version, file_info(a.setup), file_info(a.agent) if a.agent else None,
                  date=date, changelog=notes)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_bytes(page.encode("utf-8"))  # no BOM, LF only
    else:
        sys.stdout.buffer.write(page.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
