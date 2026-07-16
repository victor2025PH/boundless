# -*- coding: utf-8 -*-
"""冻结态资源定位仿真测试：模拟 PyInstaller 环境（sys.frozen + exe 目录），
验证 launcher_theme 设计令牌与 app_config.BASE（图标库定位所依赖）都指向 exe 旁的 static/。

背景（2026-07-16）：launcher_theme 旧写法按 __file__ 定位 static/design-tokens.json，
冻结态 __file__ 在临时解包目录 → 安装版静默回退旧配色，源码机永远测不出来。
本测试把这类"源码好的、装出来坏的"问题挡在门禁里。exit 0=通过 / 1=失败。
"""
import io
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent

# launcher_theme 依赖 PySide6（隔离在 .venv_launcher）；套件常用 miniconda 跑 →
# 缺 PySide6 时自动用 .venv_launcher 解释器重跑自己；连它也没有则跳过（exit 0 不拦门禁）。
if __name__ == "__main__" and "--reexeced" not in sys.argv:
    try:
        import PySide6  # noqa: F401
    except Exception:
        import subprocess
        vpy = ROOT / ".venv_launcher" / "Scripts" / "python.exe"
        if vpy.exists():
            raise SystemExit(subprocess.call([str(vpy), __file__, "--reexeced"]))
        print("SKIP: 本机无 PySide6 也无 .venv_launcher，跳过冻结态仿真")
        raise SystemExit(0)

fails = []


def ok(m):
    print(f"  [OK] {m}")


def ng(m):
    fails.append(m)
    print(f"  [NG] {m}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ah_frozen_"))
    try:
        # 仿真安装布局：{app}\AvatarHub.exe + {app}\static\{design-tokens.json, brand-icons.svg}
        (tmp / "static").mkdir()
        shutil.copy2(ROOT / "static" / "design-tokens.json", tmp / "static" / "design-tokens.json")
        shutil.copy2(ROOT / "static" / "brand-icons.svg", tmp / "static" / "brand-icons.svg")
        fake_exe = tmp / "AvatarHub.exe"
        fake_exe.write_bytes(b"stub")

        # 关键：先伪造冻结态，再导入被测模块（它们在 import 期定位资源）
        sys.frozen = True
        sys.executable = str(fake_exe)
        for m in ("launcher_theme", "app_config"):
            sys.modules.pop(m, None)
        sys.path.insert(0, str(ROOT))

        import launcher_theme as lt
        if lt.STATE_HEX["ok"].lower() == "#34d399":
            ok("冻结态 launcher_theme 从 exe 旁 static/ 读到设计令牌（状态绿=网页同款）")
        else:
            ng(f"冻结态令牌未生效：STATE_HEX.ok={lt.STATE_HEX['ok']}（回退了内置旧值?）")
        if lt.THEMES["dark"]["BG"].lower() == "#080b10":
            ok("冻结态暗色底色来自令牌")
        else:
            ng(f"冻结态暗色底色异常：{lt.THEMES['dark']['BG']}")

        import app_config
        if Path(app_config.BASE) == tmp:
            ok("冻结态 app_config.BASE = exe 目录（图标库 brand-icons.svg 按此定位）")
        else:
            ng(f"冻结态 BASE 异常：{app_config.BASE}（应为 {tmp}）")
        if (Path(app_config.BASE) / "static" / "brand-icons.svg").exists():
            ok("图标库在冻结态定位路径下存在")
        else:
            ng("冻结态定位路径下找不到 brand-icons.svg")
    finally:
        # 还原伪造，避免污染同进程后续导入（独立进程跑时无影响）
        try:
            del sys.frozen
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    print("通过" if not fails else f"失败 {len(fails)} 项")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
