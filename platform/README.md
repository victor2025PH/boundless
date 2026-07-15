# platform/ · 无界共享底座（Shared Platform Layer）

> 定位：三系七产品**共享的横切能力**的单一归属地。产品/引擎向这里"接契约"，而不是各自复制一份。
> 依赖铁律：`website / products / engines` → **可依赖 platform**；`platform` **绝不反向依赖**任何产品或引擎。
> 现状：本层是**契约 + 归属地图**（本文件）。物理代码尚分散在三个引擎里（见下表"当前实现"），按"先立契约、再逐引擎迁移、每步可回归"推进，不做一次性大爆改。

---

## 1. 五个共享契约（子目录）

| 子目录 | 契约（做什么） | 关键接口（建议签名） | 当前实现所在 | 迁移状态 |
|---|---|---|---|---|
| `identity/` | **身份 / 资产总线**：一次克隆声音+形象→跨产品复用的 Profile | `getProfile(userId)` · `bindVoice/Face(profileId, asset)` · `listAssets(profileId)` | avatarhub `avatar_hub.py`(Profile/角色库) · `profile_package.py` | 待抽取 |
| `licensing/` | **授权 / 计量 / SKU 门控**：edition→额度→按量扣减→开通 | `checkLicense(machine)` · `meter(userId, sku, units)` · `gate(feature, edition)` | avatarhub `license*.py` · chengjie `src/licensing/` · huoke（各自 gate） | 待统一 |
| `compliance/` | **合规出口**：C2PA 溯源 + Ed25519 验真 + 不可见水印 + 克隆伦理校验 | `sign(artifact)` · `watermark(media)` · `verify(artifact)` | avatarhub `provenance.py` · `watermark.py` | 待抽取 |
| `observability/` | **可观测**：KPI 埋点 / 端到端延迟 / 漏斗 / 遥测（打通 CAC/CPL/ROAS） | `emit(event, props)` · `latency(stage, ms)` · `funnel(step)` | avatarhub `metrics.py` `telemetry*.py` · chengjie `src/monitoring/` | 待统一 |
| `brand/` | **品牌设计令牌**：配色/字体/圆角/产品命名（三系七产品单一真相） | 令牌 `brand.css`/`brand.ts` · 产品表同 `website/lib/brand.ts` | website `lib/brand.ts` · avatarhub `static/brand.css` · `brand-assets/` | 待收敛为一份 |

> 产品之间**横向零 import**：任意跨产品交互都经 platform 的契约（HTTP / 注册表 / Profile 总线）完成。

---

## 2. 为什么先立契约、不先搬代码

三个引擎（avatarhub/chengjie/huoke）体量大（合计 ~2500 py + 160 tsx）、且当前**在多机在跑**（见 `deploy/` 集群拓扑）。直接把 `provenance/license/metrics` 从引擎里挖出来会牵动运行中的服务，风险高、且本地无法一键回归。故采用：

1. **立契约**（本文件）：先定义接口 + 归属，让新代码只依赖 platform 抽象。
2. **薄适配**：platform 下先放"转发到现有引擎实现"的适配层（thin adapter），产品改为依赖 platform。
3. **逐引擎迁移**：一次搬一个能力（如先 compliance），每步跑该引擎自检/回归（avatarhub `doctor.py` / chengjie `pytest` / `tools/claims_lint`）。
4. **删重复**：三处重复实现（如各自的 gate/metrics）收敛为一份后，删旧留新。

---

## 3. 落地顺序（建议）

`compliance`（无状态、最独立，先行）→ `brand`（收敛为一份令牌）→ `observability`（统一埋点 schema）→ `licensing`（统一计量表 + SKU 门控中间件）→ `identity`（资产总线，最重、最后）。

> 里程碑：`licensing` + `identity` 通了，官网 ↔ SKU ↔ license ↔ 交付 的业务闭环才真正闭合。
