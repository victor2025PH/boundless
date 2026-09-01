import { buildFaqJsonLd, type CompareFaq } from "@/lib/compare-content";

/** FAQPage JSON-LD 注入（服务端组件，实施77 渠道四）。
 *  语言随路由固定（zh 页给 zh、/en 页给 en），与页面可见 FAQ 同源，
 *  防「schema 说 A 页面显 B」的口径分叉。 */
export default function FaqJsonLd({ faq, lang }: { faq: readonly CompareFaq[] | CompareFaq[]; lang: "zh" | "en" }) {
  return (
    <script
      type="application/ld+json"
      dangerouslySetInnerHTML={{ __html: JSON.stringify(buildFaqJsonLd([...faq], lang)) }}
    />
  );
}
