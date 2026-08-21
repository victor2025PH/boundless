import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import EnterprisePage from "@/components/EnterprisePage";
import { SITE_URL } from "@/lib/site";
import { enterpriseFaqJsonLd, enterpriseServiceJsonLd } from "@/lib/enterpriseContent";

const LANGUAGES = { "zh-CN": "/enterprise", en: "/en/enterprise", "x-default": "/enterprise" };

export const metadata: Metadata = {
  title: "企业服务 · 年框合作与私有化部署 · 智聊 ChatX · 无界科技 BOUNDLESS",
  description:
    "把 AI 成交聊天开进你的组织：企业合作年框（云端托管、协议价、月结对公发票）或企业级私有化部署（整套引擎进内网、数据不出网、本地模型 Token 不限量、一次性实施 + 年授权维保）。提交需求 1 个工作日内商务回复。",
  alternates: { canonical: "/enterprise", languages: LANGUAGES },
  openGraph: {
    title: "企业服务 · 年框合作 / 私有化部署 · 无界科技",
    description:
      "同一套 AI 成交聊天引擎，两种企业形态：云端年框省心，私有化部署数据不出网。需求勘察 → PoC → 部署 → 陪跑，典型 1–2 周上线。",
    url: `${SITE_URL}/enterprise`,
  },
};

export default function EnterpriseRoute() {
  return (
    <main className="relative min-h-screen">
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(enterpriseServiceJsonLd("zh")) }}
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(enterpriseFaqJsonLd("zh")) }}
      />
      <Navbar />
      <EnterprisePage />
      <Footer />
    </main>
  );
}
