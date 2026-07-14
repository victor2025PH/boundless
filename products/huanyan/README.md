# 幻颜 FaceX（huanyan）

- 产品系：幻境系 Studio（破容貌/声音/身份之界）
- 承载引擎：`engines/avatarhub`（faceswap 图/视频换脸 + GFPGAN/CodeFormer + 发型/妆容/试衣）
- 定位：离线图片 / 视频影视级换脸 + 双人 face_map + 开播前定妆
- 合规可见性：🔴 隔离 / 准入（深度伪造监管，授权确认 + 水印 + 用途承诺）
- 产品化封装（本目录）：SKU/定价（定制交付）· 落地页引用 · 交付包 · 授权声明

> 本目录为产品化薄封装，复用 `engines/` 共享实现；产品间横向零 import，只经 `platform`（身份/授权/注册表/HTTP 契约）交互。
