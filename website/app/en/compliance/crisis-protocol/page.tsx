import type { Metadata } from "next";
import LegalShell from "@/components/LegalShell";
import { crisisProtocolSections, crisisProtocolTitle, LEGAL_UPDATED } from "@/lib/legal-content";

export const metadata: Metadata = {
  title: "Crisis Intervention Protocol · BOUNDLESS",
  description:
    "Crisis intervention protocol template for the ChatX AI companion service: detection, intervention, referral resources and annual-report counters (SB 243 companion page).",
  alternates: {
    canonical: "/en/compliance/crisis-protocol",
    languages: {
      "zh-CN": "/compliance/crisis-protocol",
      en: "/en/compliance/crisis-protocol",
      "x-default": "/compliance/crisis-protocol",
    },
  },
  robots: { index: true, follow: true },
};

export default function CrisisProtocolPageEn() {
  return (
    <LegalShell
      title={crisisProtocolTitle}
      updated={LEGAL_UPDATED}
      sections={crisisProtocolSections}
    />
  );
}
