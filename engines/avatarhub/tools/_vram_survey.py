# -*- coding: utf-8 -*-
"""One-shot: query VRAM usage across all cluster hosts via nvidia-smi (local + SSH)."""
import base64
import re
import subprocess

rows = []
out = subprocess.run(
    ['nvidia-smi', '--query-gpu=memory.used,memory.total', '--format=csv,noheader,nounits'],
    capture_output=True, text=True).stdout.strip()
u, t = [int(x) for x in out.split(',')]
rows.append(('.176 (5090)', u, t))

for host, label in [('192.168.0.140', '.140 (4070)'), ('192.168.0.198', '.198 (4070)'),
                    ('192.168.0.117', '.117 (3060)'), ('192.168.0.104', '.104 (4070)')]:
    ps = 'nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits'
    b64 = base64.b64encode(ps.encode('utf-16-le')).decode()
    try:
        r = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=8', f'Administrator@{host}',
             f'powershell -NoProfile -EncodedCommand {b64}'],
            capture_output=True, text=True, timeout=25)
        m = re.search(r'(\d+),\s*(\d+)', r.stdout)
        rows.append((label, int(m.group(1)), int(m.group(2))) if m else (label, -1, -1))
    except Exception:
        rows.append((label, -1, -1))

for label, u, t in rows:
    pct = f'{u / t * 100:.0f}%' if t > 0 else 'ERR'
    print(f'{label:14s} {u:6d}/{t:6d} MiB  {pct}')
