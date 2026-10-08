# -*- coding: utf-8 -*-
"""Point 173 at the redesigned group-member page. Do not replace whole route files."""
from pathlib import Path

root = Path(r"D:\workspace\boundless\engines\chengjie\src\web")

route = root / "routes" / "group_members_routes.py"
text = route.read_text(encoding="utf-8")
old = 'request, "tg_members_shell.html",'
new = 'request, "tg_members.html",'
if old not in text:
    raise SystemExit("route still pointing at shell: marker missing")
route.write_text(text.replace(old, new, 1), encoding="utf-8")
print("route ok")

pack = root / "i18n_packs" / "group_members.py"
pt = pack.read_text(encoding="utf-8")
repls = [
    (
        "多号进群 → 限速拉「发言且非管理员」的人入库 → 每日控量。只读提取；私聊触达是独立步骤。",
        "先在群里接有意向的人的话。提取只是把人放进库，不发消息。",
    ),
    (
        "Multi-account join -> rate-limited pull of active non-admins -> daily cap. Read-only; outreach is a separate step.",
        "Reply in the group first. Extraction only puts people in the library and sends nothing.",
    ),
]
for src, dst in repls:
    if src not in pt:
        print("pack missing:", src[:24])
    else:
        pt = pt.replace(src, dst)
        print("pack replaced:", src[:24])
pack.write_text(pt, encoding="utf-8")

inbox = root / "templates" / "unified_inbox.html"
it = inbox.read_text(encoding="utf-8")
old_i, new_i = repls[0]
if old_i in it:
    inbox.write_text(it.replace(old_i, new_i), encoding="utf-8")
    print("inbox ok")
else:
    print("inbox marker missing")

shell = root / "templates" / "tg_members_shell.html"
if shell.exists():
    shell.unlink()
    print("shell removed")
else:
    print("shell already gone")

page = (root / "templates" / "tg_members.html").read_text(encoding="utf-8")
print("page extends", page.startswith('{% extends "base.html" %}'))
print("page gtouch", 'id="gm-gtouch"' in page)
