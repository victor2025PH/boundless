# platform/compliance · 抽取迁移手册（在装有 avatarhub env 的机器上执行）

> 状态：**已就绪、待在 env 机执行**。已在开发机 `facefusion` env 实测：`import provenance` OK、`import watermark` OK（两模块可独立导入）——故下述"移动 + re-export shim"方案成立。
> 为什么不在无 env 处直接做：avatarhub 跨 conda env/多机运行，抽取后需 `doctor.py` 回归验证；无 env 处改了无法验证 = 违反"每步可回归"。

## 目标
把合规能力（`provenance.py` C2PA/Ed25519/伦理校验、`watermark.py` 不可见水印）从 `engines/avatarhub/` 收敛为 `platform/compliance/`，成为全域共享单一实现；avatarhub 保留 1 行 re-export 兼容，**零行为变化**。

## 步骤（在 `D:\workspace\wujie`，用 avatarhub 的 python）

```powershell
$py = "<CONDA>\envs\facefusion\python.exe"     # avatarhub 运行解释器
cd D:\workspace\wujie

# 1) 移动 canonical 实现到 platform/compliance（git mv 保留跟踪）
git mv engines/avatarhub/provenance.py platform/compliance/provenance.py
git mv engines/avatarhub/watermark.py  platform/compliance/watermark.py
New-Item platform/compliance/__init__.py -ItemType File -Force

# 2) 让 avatarhub 能 import platform：在 app_config.py 顶部插入仓根/platform 上 sys.path
#    （app_config 已是所有服务的公共入口，改这一处即全服务生效）
#    追加：
#      import sys, pathlib
#      _REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]   # engines/avatarhub -> wujie
#      sys.path.insert(0, str(_REPO_ROOT))

# 3) 在 avatarhub 留兼容 shim（旧 import 不动即可跑）
#    engines/avatarhub/provenance.py:
#      from platform.compliance.provenance import *   # noqa (compat shim, canonical in platform/)
#    engines/avatarhub/watermark.py:
#      from platform.compliance.watermark import *     # noqa

# 4) 回归验证（关键闸门）
& $py app_config.py                       # 路径/env 解析仍全绿
& $py -c "import provenance, watermark; print('shim import OK')"
& $py -c "import sys; sys.path.insert(0,'.'); from platform.compliance import provenance, watermark; print('platform import OK')"
& $py doctor.py                           # 服务拉起后全绿（HD/克隆/水印链路不回归）
& $py tools\claims_lint.py                # 合规宣称门禁仍过

# 5) 绿了再收口：确认无其它引擎/脚本直接文件路径引用后，可选删 shim、改为直接 import platform.compliance
```

## 回退
`git mv` 可逆；未删原逻辑（仅移动+加 shim）。任一步 `doctor.py` 报红 → `git reset --hard HEAD~1` 即回退。

## 之后的同形迁移（同一手册套用）
`brand`（令牌收敛一份）→ `observability`（统一埋点 schema）→ `licensing`（计量表 + SKU 门控中间件，消费 products/*/product.yaml 的 skus）→ `identity`（资产总线，最后）。
