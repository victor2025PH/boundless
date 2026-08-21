import type { Metadata } from "next";
import LegalShell from "@/components/LegalShell";
import {
  complianceCapabilitySections,
  complianceCapabilityTitle,
} from "@/lib/legal-content";

// P2 销售件（2026-08-18）：合规能力页——三部已生效法规 → 产品开关与证据面。
// 销售直接发本页链接（替代 md 附件）；内容单源 lib/legal-content.ts。
const UPDATED = "2026-08-18";

export const metadata: Metadata = {
  title: "合规能力 EU AI Act / SB 243 / GBL §1700 · 智聊 ChatX · 无界科技 BOUNDLESS",
  description:
    "智聊 ChatX 把 EU AI Act 第 50 条、加州 SB 243、纽约 GBL §1700 的披露、诚实身份、危机干预与年报计数做成产品开关与可导出证据面：九语披露语、危机识别闭环、转介计数导出、公示页模板，默认全关由运营方 opt-in。",
  alternates: {
    canonical: "/compliance",
    languages: {
      "zh-CN": "/compliance",
      en: "/en/compliance",
      "x-default": "/compliance",
    },
  },
  robots: { index: true, follow: true },
};

export default function CompliancePage() {
  return (
    <LegalShell
      title={complianceCapabilityTitle}
      updated={UPDATED}
      sections={complianceCapabilitySections}
    />
  );
}
