import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import ZhituoDownloadSection from "@/components/ZhituoDownloadSection";
import { SITE_URL } from "@/lib/site";

// 2026-10-11: official Zhituo download page (public; linked from the download menu and hub).
export const metadata: Metadata = {
  title: "Download the Zhituo Client · BOUNDLESS",
  description:
    "Download Zhituo (Windows): on-prem multi-account operations client, one-shot install, account-based auto-enrollment, deployed locally with data on-device. Step-by-step guide, SHA-256 verifiable.",
  alternates: {
    canonical: "/en/download/zhituo",
    languages: { "zh-CN": "/download/zhituo", en: "/en/download/zhituo", "x-default": "/download/zhituo" },
  },
  openGraph: {
    title: "Download the Zhituo Client · BOUNDLESS",
    description: "On-prem multi-account operations client: Windows available, deployed locally, data on-device.",
    url: `${SITE_URL}/en/download/zhituo`,
  },
};

export default function ZhituoDownloadPageEn() {
  return (
    <main className="relative min-h-screen">
      <Navbar />
      <ZhituoDownloadSection lang="en" />
      <Footer />
    </main>
  );
}