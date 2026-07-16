# 无界科技 BOUNDLESS · 全域单仓 wujie

母品牌无界 BOUNDLESS 旗下「三系七产品 + 官网 + 品牌库 + 文档」的统一单仓。

## 结构

- `platform/` 共享底座：品牌令牌 / 身份·资产总线 / 授权计量 / 合规 / 可观测
- `engines/` 3 套自研核心引擎（多产品共享，一次维护）
  - `avatarhub` 换脸·克隆·数字人·同传（原「模仿音色 / mfys」）
  - `chengjie` 客服承接·统一收件箱·翻译栈·陪伴（原 telegram-mtproto-ai）
  - `huoke` 真机 RPA 获客（原 mobile-auto0423）
- `products/` 7 条产品线（拼音文件夹，产品化薄封装，复用 engines）
  - `zhituo` 智拓 ReachX · `zhiliao` 智聊 ChatX · `tongyi` 通译 LingoX · `tongchuan` 通传 VoxX
  - `huansheng` 幻声 VoiceX · `huanying` 幻影 LiveX · `huanyan` 幻颜 FaceX
- `website/` 官网 bd2026（Next.js 统一门面；主域 bd2026.cc）
- `brand-assets/` 品牌资产库
- `docs/` 商业 / 战略 / 实施 / 运维文档
- `vendor/` 第三方引擎（index-tts 等，不入库，按需部署）
- `deploy/ tools/ packaging/` 多机部署 / 门禁(claims_lint,gate) / 打包

## 依赖规则

`website / products` → `engines` → `platform`；产品间横向零 import，只经 platform（身份/授权/注册表/HTTP 契约）交互。

## 合规分层

合规主站（通译/智聊企业版/幻声/数字人/同传）+ 隔离准入区（换脸/RPA/陪伴/无审查）；母品牌统一背书，支付主体/域名/可索引页面分离。

> 整理方案详见 `docs/` 下《全域项目整理方案_BOUNDLESS_三视角》。本仓由整理迁移生成，历史采「拉净码重开」（不并入各源仓旧提交）。
