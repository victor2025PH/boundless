# -*- coding: utf-8 -*-
"""六机台账定时巡检：machines_lint --remote 全量对账，红灯走 alerts 推送。

计划任务 BoundlessMachinesPatrol 每天 09:30 跑（工作时间推送才有人看；
pre-push 门禁只做文件级检查，远端一致性由本巡检兜底）。
红灯 → alerts.raise_alert（去抖）；恢复 → clear_alert 报平安。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AVATARHUB = Path(r"C:\模仿音色")
PY = sys.executable

try:
    sys.path.insert(0, str(AVATARHUB))
    import alerts as _alerts
except Exception:
    _alerts = None

KEY = "cluster_machines_lint"


def main() -> int:
    r = subprocess.run(
        [PY, str(ROOT / "tools" / "machines_lint.py"), "--remote", "--strict"],
        capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace",
    )
    out = (r.stdout or "") + (r.stderr or "")
    print(out.strip())
    if r.returncode == 0:
        if _alerts is not None:
            try:
                _alerts.clear_alert(KEY, note="六机台账对账恢复全绿")
            except Exception:
                pass
        return 0
    reds = [l for l in out.splitlines() if "[RED]" in l][:5]
    if _alerts is not None:
        try:
            _alerts.raise_alert(
                KEY, "六机台账对账红灯（machines_lint --remote）",
                detail="；".join(reds) or out[-300:],
                level="warn", source="machines_patrol",
            )
        except Exception:
            pass
    return 1


if __name__ == "__main__":
    sys.exit(main())
