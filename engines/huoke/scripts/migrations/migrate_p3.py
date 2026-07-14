import sqlite3, os, json, re, sys

BASE = r"D:\mobile-auto-0327\mobile-auto-project\data"

def norm(s):
    s = (s or "").lower().strip()
    s = re.sub(r"^@+", "", s)
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", s)
    return s

conn = sqlite3.connect(os.path.join(BASE, "openclaw.db"))
conn.execute("""
CREATE TABLE IF NOT EXISTS device_daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    date TEXT NOT NULL,
    sessions_count INTEGER DEFAULT 0,
    videos_watched INTEGER DEFAULT 0,
    follows_count INTEGER DEFAULT 0,
    dms_sent INTEGER DEFAULT 0,
    dms_responded INTEGER DEFAULT 0,
    leads_qualified INTEGER DEFAULT 0,
    algo_score REAL DEFAULT 0,
    online_minutes INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(device_id, date)
)
""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_dds_device_date ON device_daily_stats(device_id, date)")
conn.commit()
print("openclaw.db: device_daily_stats OK")

conn2 = sqlite3.connect(os.path.join(BASE, "leads.db"))
cols = [c[1] for c in conn2.execute("PRAGMA table_info(platform_profiles)").fetchall()]
if "device_id" not in cols:
    conn2.execute("ALTER TABLE platform_profiles ADD COLUMN device_id TEXT")
    print("leads.db: added device_id to platform_profiles")
else:
    print("leads.db: platform_profiles.device_id already exists")

cols_int = [c[1] for c in conn2.execute("PRAGMA table_info(interactions)").fetchall()]
if "device_id" not in cols_int:
    conn2.execute("ALTER TABLE interactions ADD COLUMN device_id TEXT")
    print("leads.db: added device_id to interactions")
else:
    print("leads.db: interactions.device_id already exists")
conn2.commit()

conn3 = sqlite3.connect(os.path.join(BASE, "openclaw.db"))
tasks = conn3.execute("SELECT device_id, params FROM tasks WHERE type='tiktok_send_dm' AND device_id IS NOT NULL AND params IS NOT NULL").fetchall()
username_to_device = {}
for device_id, params_str in tasks:
    try:
        params = json.loads(params_str)
        recipient = params.get("recipient") or params.get("target_user") or ""
        if recipient:
            username_to_device[norm(recipient)] = device_id
    except Exception:
        pass
print(f"Found {len(username_to_device)} username->device mappings")

if username_to_device:
    profiles = conn2.execute("SELECT id, username FROM platform_profiles WHERE device_id IS NULL").fetchall()
    updated = 0
    for pid, username in profiles:
        key = norm(username)
        if key in username_to_device:
            conn2.execute("UPDATE platform_profiles SET device_id=? WHERE id=?", (username_to_device[key], pid))
            updated += 1
    conn2.commit()
    print(f"Backfilled {updated} platform_profiles")

conn3.close()
conn2.close()
conn.close()
print("Migration P3 complete.")
