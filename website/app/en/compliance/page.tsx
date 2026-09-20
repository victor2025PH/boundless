import type { Metadata } from "next";
import LegalShell from "@/components/LegalShell";
import {
  complianceCapabilitySections,
  complianceCapabilityTitle,
} from "@/lib/legal-content";

const UPDATED = "2026-08-18";

export const metadata: Metadata = {
  title: "Compliance capabilities — EU AI Act / CA SB 243 / NY GBL §1700 · ChatX · BOUNDLESS",
  description:
    "ChatX ships EU AI Act Art. 50, California SB 243 and New York GBL §1700 duties as product switches with exportable evidence: nine-language disclosure, honest-identity mode, a continuously tested crisis-intervention chain, referral-count export and a protocol template page. All off by default; operators opt in.",
  alternates: {
    canonical: "/en/compliance",
    languages: {
      "zh-CN": "/compliance",
      en: "/en/compliance",
      "x-default": "/compliance",
    },
  },
  robots: { index: true, follow: true },
};

export default function CompliancePageEn() {
  return (
    <LegalShell
      title={complianceCapabilityTitle}
      updated={UPDATED}
      sections={complianceCapabilitySections}
    />
  );
}
