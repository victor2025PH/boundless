/** 通用 FAQPage JSON-LD 构造（实施77 GEO 批次2）。
 *  纪律：只对「页面上真实可见的 FAQ」生成 schema，语言随路由固定。 */
export function faqPageJsonLd(items: ReadonlyArray<{ q: string; a: string }>): object {
  return {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: items.map((it) => ({
      "@type": "Question",
      name: it.q,
      acceptedAnswer: { "@type": "Answer", text: it.a },
    })),
  };
}

/** 把页面文案里的行内 Markdown 拆成纯文本（实施77 GEO 批次3）。
 *
 *  为什么必须做：站内 FAQ 文案带 `**加粗**` 与 `` `代码` `` 标记，页面渲染时会被转成样式，
 *  但塞进 JSON-LD 就是字面量——AI 引擎整段引用时会把星号一起念出来。schema 的值是给机器读的
 *  纯文本，不是 Markdown 源码。
 *
 *  刻意保守：只处理**行内**强调/代码/链接三种，不碰列表、标题、换行——FAQ 答案是散文，
 *  过度「清洗」反而会吃掉内容。 */
export function plainText(md: string): string {
  return md
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1") // 图片 → alt
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1") // 链接 → 文字
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1$2")
    .replace(/\s+/g, " ")
    .trim();
}
