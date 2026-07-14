# 幻声 VoiceX（huansheng）

- 产品系：幻境系 Studio（破容貌/声音/身份之界）
- 承载引擎：`engines/avatarhub`（fish/voxcpm/qwen3/sbv2/emotion TTS + RVC 变声）+ `vendor/index-tts`
- 定位：零样本声音克隆 + 多语种 TTS + 实时变声 + 唱歌工作室
- 合规可见性：🟢 主站（授权音色声明 + 知情同意）
- 产品化封装（本目录）：SKU/定价 · 免费试听（水印/截断）· 落地页引用 · 交付包

> 本目录为产品化薄封装，复用 `engines/` 共享实现；产品间横向零 import，只经 `platform`（身份/授权/注册表/HTTP 契约）交互。
