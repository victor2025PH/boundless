import type { Metadata } from "next";
import LegalShell from "@/components/LegalShell";
import { crisisProtocolSections, crisisProtocolTitle, LEGAL_UPDATED } from "@/lib/legal-content";

export const metadata: Metadata = {
  title: "危机干预协议 Crisis Intervention Protocol · 无界科技 BOUNDLESS",
  description:
    "智聊 ChatX AI 陪伴服务的危机干预协议模板：危机识别、干预措施、转介资源与年度报告口径（SB 243 公示义务配套）。",
  alternates: {
    canonical: "/compliance/crisis-protocol",
    languages: {
      "zh-CN": "/compliance/crisis-protocol",
      en: "/en/compliance/crisis-protocol",
      "x-default": "/compliance/crisis-protocol",
    },
  },
  robots: { index: true, follow: true },
};

export default function CrisisProtocolPage() {
  return (
    <LegalShell
      title={crisisProtocolTitle}
      updated={LEGAL_UPDATED}
      sections={crisisProtocolSections}
    />
  );
}
