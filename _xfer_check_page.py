from pathlib import Path
t = Path(r"D:\workspace\boundless\engines\chengjie\src\web\templates\tg_members.html").read_text(encoding="utf-8")
print("extends", t.startswith("{% extends"))
print("labelOption", "labelOption" in t)
print("scoped_card", ".gm-page .card" in t)
print("reply_label", "用这个号在群里接话" in t)
