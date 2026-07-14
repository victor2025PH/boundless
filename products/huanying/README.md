# 幻影 LiveX（huanying）

- 产品系：幻境系 Studio（破容貌/声音/身份之界）
- 承载引擎：`engines/avatarhub`（lipsync 活体口型 + vcam WebRTC/OBS + 实时换脸 + 虚拟背景）
- 定位：实时数字人 / 虚拟主播 + 直播实时换脸换声
- 合规可见性：🟢 数字人主站 / 🔴 直播换脸准入
- 产品化封装（本目录）：SKU/定价 · 硬件档位引导 · 落地页引用 · 交付包

> 本目录为产品化薄封装，复用 `engines/` 共享实现；产品间横向零 import，只经 `platform`（身份/授权/注册表/HTTP 契约）交互。
