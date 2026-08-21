// /enterprise 页 FAQ 单源：页面渲染与 FAQPage JSON-LD 共用（growthContent 同款姿势）。
import { SITE_URL } from "./site";

export interface EnterpriseFaq {
  q: { zh: string; en: string };
  a: { zh: string; en: string };
}

export const ENTERPRISE_FAQ: EnterpriseFaq[] = [
  {
    q: { zh: "年框和私有化怎么选？", en: "Frame deal or private deployment?" },
    a: {
      zh: "数据必须留在内网、或有行业合规硬性要求 → 私有化；否则年框更轻：云端托管当天开通，量大按协议价结算，后续随时可升级成私有化。",
      en: "If data must stay on-prem or compliance demands it, go private. Otherwise the frame deal is lighter: managed cloud, live the same day, and you can upgrade to private later.",
    },
  },
  {
    q: { zh: "私有化对硬件有什么要求？", en: "Hardware requirements for private deployment?" },
    a: {
      zh: "一台带 NVIDIA GPU 的服务器起步（4090 级即可流畅跑本地模型，Token 不限量）；没有 GPU 也能部署，AI 部分走你自己的云端 Key。",
      en: "One server with an NVIDIA GPU (a 4090-class card runs local models smoothly, tokens unlimited). No GPU? It still deploys — AI runs on your own cloud keys.",
    },
  },
  {
    q: { zh: "交付要多久？", en: "How long until we're live?" },
    a: {
      zh: "年框当天开通；私有化典型 1–2 周：需求勘察 → PoC 试点 → 部署验收 → 团队培训，全程有实施清单。",
      en: "Frame deals go live the same day. Private deployments typically take 1–2 weeks: survey → PoC → deploy & accept → training.",
    },
  },
  {
    q: { zh: "团队现有账号的充值余额怎么办？", en: "What happens to our existing top-up balance?" },
    a: {
      zh: "并入企业协议，余额不作废：实付充值 Token 本就长期有效，签约时按协议口径折算或继续使用。",
      en: "It folds into the enterprise agreement — nothing is forfeited. Paid tokens stay valid and convert on agreed terms.",
    },
  },
  {
    q: { zh: "怎么报价？", en: "How is pricing quoted?" },
    a: {
      zh: "企业合作按年用量阶梯（建议 ≥20,000U 起谈）；私有化 = 一次性实施 + 年授权维保，按席位与算力规模报价。提交表单后 1 个工作日内商务回复。",
      en: "Frame deals tier by annual volume (from ~20,000U). Private = one-time setup + annual license & care, quoted by seats and hardware scale. Sales replies within 1 business day.",
    },
  },
];

export function enterpriseFaqJsonLd(lang: "zh" | "en") {
  return {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: ENTERPRISE_FAQ.map((f) => ({
      "@type": "Question",
      name: f.q[lang],
      acceptedAnswer: { "@type": "Answer", text: f.a[lang] },
    })),
  };
}

/** Service JSON-LD（面议不报价——刻意不带 offers.price，只声明可询价）。 */
export function enterpriseServiceJsonLd(lang: "zh" | "en") {
  const zh = lang === "zh";
  const url = `${SITE_URL}${zh ? "" : "/en"}/enterprise`;
  return {
    "@context": "https://schema.org",
    "@type": "Service",
    name: zh ? "智聊 ChatX 企业服务（年框合作 / 私有化部署）" : "ChatX enterprise (frame deals / private deployment)",
    description: zh
      ? "AI 成交聊天系统的企业形态：云端年框协议价，或整套引擎私有化部署进企业内网（本地模型 Token 不限量）。"
      : "Enterprise shapes of the AI closing-chat system: managed cloud frame deals, or full private deployment on-prem with unlimited local-model tokens.",
    provider: { "@type": "Organization", name: zh ? "无界科技 BOUNDLESS" : "BOUNDLESS" },
    url,
  };
}
