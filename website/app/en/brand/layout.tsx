import type { Metadata } from "next";
import { FAMILY_PITCH } from "@/lib/brand";

// 同 app/brand/layout.tsx：描述文案复用 FAMILY_PITCH.en.sub 单一真相，见其注释。
export const metadata: Metadata = {
  title: "Brand Story · BOUNDLESS",
  description: `BOUNDLESS brand story: ${FAMILY_PITCH.en.sub}`,
  alternates: {
    canonical: "/en/brand",
    languages: { "zh-CN": "/brand", en: "/en/brand" },
  },
  openGraph: {
    type: "website",
    url: "/en/brand",
    title: "Brand Story · BOUNDLESS",
    description: "Communication, Boundless. We tear down seven walls with AI.",
    siteName: "BOUNDLESS",
  },
  robots: { index: true, follow: true },
};

export default function BrandEnLayout({ children }: { children: React.ReactNode }) {
  return children;
}
