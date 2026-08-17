"""SOP step-1 quiet check: newest mtime under the trees that get packed."""
import os
import time

ROOT = r"d:\boundless\engines\chengjie"
TREES = ["services/messenger-web", "services/whatsapp-baileys",
         "../../shared" if False else "shared/inject", "desktop"]
SKIP = ("node_modules", ".git", "dist", "dist-1019", "dist-release", "logs",
        "sessions", "temp", "__pycache__", "build-info.json")

now = time.time()
for tree in TREES:
    base = os.path.join(ROOT, tree.replace("/", os.sep))
    newest, npath = 0.0, ""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for f in filenames:
            if f in SKIP:
                continue
            p = os.path.join(dirpath, f)
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if m > newest:
                newest, npath = m, p
    age = (now - newest) / 60 if newest else -1
    print("%-32s newest %6.1f min ago  %s" % (
        tree, age, os.path.relpath(npath, base) if npath else "(empty)"))
