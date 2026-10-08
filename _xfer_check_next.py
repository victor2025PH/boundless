from pathlib import Path
root = Path(r"D:\workspace\boundless\engines\chengjie\src\web\templates")
gm = (root / "tg_members.html").read_text(encoding="utf-8")
ps = (root / "personas.html").read_text(encoding="utf-8")
print("reply", "用这个号在群里接话" in gm)
print("href", "focus=pe-sales-kw" in gm)
print("hash", "pe-sales-kw" in ps and "_pendingHashFocus" in ps)
