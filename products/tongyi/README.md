# 通译 LingoX（tongyi）

- 产品系：通达系 Lingo（破语言之界）
- 承载引擎：`engines/chengjie`（翻译栈子集，overlay 裁剪为纯翻译工作台）
- 定位：跨境实时双向翻译 SCRM（术语强制 + 翻译记忆 + 客户资产沉淀）
- 合规可见性：🟢 主站 · **首屏主推现金流**

## 本目录（产品化薄封装 · 其它 6 产品照此模板）

- `product.yaml` — **产品清单单一真相**：SKU/定价占位、承载引擎、裁剪 overlay、官网落地页映射、合规可见性、交付与计量。
- 复用而非复制：翻译能力在 `engines/chengjie`；裁剪预设在 `engines/chengjie/config/lingox.overlay.example.yaml`；官网卡在 `website/lib/content.ts#translate`（已同步为「聊天翻译 SCRM」+ 占位价）。

> 依赖铁律：产品间横向零 import，只经 `platform`（身份/授权/合规/可观测）与 `engines`（HTTP/overlay）交互。
> 待回填：`product.yaml` 里 3 个 SKU 的 `price: TBD`（建议值见 `docs/` 实施01）。
