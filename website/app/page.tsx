import SiteHome from "@/components/SiteHome";
import { content } from "@/lib/content";
import { faqPageJsonLd } from "@/lib/jsonld";

// 首页 FAQPage schema（原在根 layout 全站注入英文版，2026-08-27 归位：
// 本页可见 FAQ（Faq 组件，zh 路由渲染中文条目）→ schema 同源同语言。
const faqLd = faqPageJsonLd(content.zh.faq.items);

export default function Home() {
  return (
    <>
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(faqLd) }} />
      <SiteHome />
    </>
  );
}
