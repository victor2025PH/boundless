# 通传 VoxX（tongchuan）

- 产品系：通达系 Lingo（破语言之界）
- 承载引擎：`engines/avatarhub`（`live_interpreter.py` 实时同传 + 字幕）
- 定位：会议 / 直播实时语音同声传译（克隆音同传 + 双语字幕 + 抢话打断）
- 合规可见性：🟢 主站
- 产品化封装（本目录）：SKU/定价 · 通话套餐预设 · 落地页引用 · 交付包 · 产品文档

> 本目录为产品化薄封装，复用 `engines/` 共享实现；产品间横向零 import，只经 `platform`（身份/授权/注册表/HTTP 契约）交互。
