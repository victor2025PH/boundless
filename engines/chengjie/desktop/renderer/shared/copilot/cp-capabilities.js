"use strict";

// 副驾「能力橱窗」数据（纯函数，单一事实源；两端共享）。
// 消费方：① 桌面壳原生空态（renderer.js::renderCopilotShowcase，经 index.html 相对路径加载）
//        ② 统一 App 空态卡（shared/copilot/app.html，no-ctx 时在「回复台」空态下方展示，
//           web 经 /copilot/cp-capabilities.js 同源加载）。
// 双模式：浏览器经 <script> 加载后 capabilityShowcase 成为全局；
//        Node 经 require 取 module.exports（desktop/test/copilot-capabilities.test.js 单测）。
// 2026-08-12 自 desktop/renderer/copilot-capabilities.js 迁入 shared/copilot/（单源供两端），
// 并补 *_en 字段供统一 App 英文档；zh 文案与既有门禁关键词保持逐字兼容。
//
// 存在理由：右栏副驾的空状态（没打开会话时）是产品的「第一印象」却被浪费。对标翻译型
// 竞品（云译只做翻译），我们的差异化——**AI 人设拟稿 / 受控出站人审 / 知识库 / 关系阶段
// 画像 / 语音克隆**——恰恰在这一屏一个都没露出来。把它做成一张简洁的能力橱窗，命名的
// 正是竞品没有的东西：让人一眼看到「不止翻译」。
//
// 门禁（copilot-capabilities.test.js）钉住条目必须覆盖这些**真差异化**关键词，防哪天被
// 改成「一键翻译 / 快速回复」这类和竞品同质的空话。

function capabilityShowcase() {
  return [
    {
      key: "draft",
      title: "AI 拟稿 · 人设一致",
      desc: "按你的人设口吻自动拟回复，禁客服腔——不只是翻译对方的话",
      title_en: "AI drafts · on persona",
      desc_en: "Replies drafted in your persona's own voice — more than translating",
    },
    {
      key: "outbound",
      title: "受控出站 · 人审把关",
      desc: "AI 稿先过风控规则与人工审核，再一键发出，绝不失控直发",
      title_en: "Guarded outbound · human review",
      desc_en: "AI drafts pass risk rules and human review before sending",
    },
    {
      key: "kb",
      title: "知识库话术",
      desc: "常见问题一键检索插入，多号回复口径专业一致",
      title_en: "Knowledge-base answers",
      desc_en: "Search and insert vetted answers; consistent across accounts",
    },
    {
      key: "relation",
      title: "关系阶段 · 客户档案",
      desc: "自动识别客户关系阶段、沉淀画像，跟进有据可依",
      title_en: "Relationship stages · profiles",
      desc_en: "Auto-tracked relationship stage and customer profile",
    },
    {
      key: "voice",
      title: "语音克隆回复",
      desc: "用人设声线发语音，情绪价值不止于文字",
      title_en: "Cloned-voice replies",
      desc_en: "Send voice notes in your persona's cloned voice",
    },
  ];
}

// 橱窗顶部一句话定位（与条目分开,便于门禁单独钉「不止翻译」这个对标点）。
// lang 可选："en" 出英文，其余一律中文（缺省行为与迁移前零参调用兼容）。
function showcaseHeadline(lang) {
  return lang === "en"
    ? "What this sidebar does for you · beyond translation"
    : "这个侧栏能替你做的 · 不止翻译";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { capabilityShowcase, showcaseHeadline };
}
