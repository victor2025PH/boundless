# 通译 LingoX（tongyi）

- 产品系：通达系 Lingo（破语言之界）
- 承载引擎：`engines/chengjie`（翻译栈子集，overlay 裁剪为纯翻译工作台）
- 定位：跨境实时双向翻译 SCRM（术语强制 + 翻译记忆 + 客户资产沉淀）
- 合规可见性：🟢 主站 · **首屏主推现金流**
- 产品化封装（本目录）：`lingox.overlay.yaml` 裁剪预设 · SKU/定价（字符包/订阅）· 落地页 · 轻量交付包

> 本目录为产品化薄封装，复用 `engines/` 共享实现；产品间横向零 import，只经 `platform`（身份/授权/注册表/HTTP 契约）交互。
