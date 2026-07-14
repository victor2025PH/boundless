# 智聊 ChatX（zhiliao）

- 产品系：智连系 Growth（破沟通与成交之界）
- 承载引擎：`engines/chengjie`（客服承接/统一收件箱/AI回复，原 telegram-mtproto-ai）
- 定位：多平台统一收件箱 + AI 人设承接 + 翻译 + 转人工，转化承接层（护城河）
- 合规可见性：🟠 主站（官方 API 企业版）/ 🔴 准入（个人号版）
- 产品化封装（本目录）：SKU/定价 · overlay 裁剪 · 落地页引用 · 交付包 · 产品文档

> 本目录为产品化薄封装，复用 `engines/` 共享实现；产品间横向零 import，只经 `platform`（身份/授权/注册表/HTTP 契约）交互。
