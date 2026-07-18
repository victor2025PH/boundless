import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Brand Story · BOUNDLESS",
  description:
    "BOUNDLESS brand story: AI that breaks the barriers of reach, closing, face, voice, identity and language — seven product lines from lead-gen to close.",
  alternates: {
    canonical: "/en/brand",
    languages: { "zh-CN": "/brand", en: "/en/brand" },
  },
  openGraph: {
    type: "website",
    url: "/en/brand",
    title: "Brand Story · BOUNDLESS",
    description: "Communication, Boundless. We tear down six walls with AI.",
    siteName: "BOUNDLESS",
  },
  robots: { index: true, follow: true },
};

export default function BrandEnLayout({ children }: { children: React.ReactNode }) {
  return children;
}
