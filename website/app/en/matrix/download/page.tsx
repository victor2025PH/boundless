import type { Metadata } from "next";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import MatrixxDownloadSection from "@/components/MatrixxDownloadSection";
import { SITE_URL } from "@/lib/site";
import { MATRIXX } from "@/lib/matrixxContent";

export const metadata: Metadata = {
  title: "Download MatrixX Client · BOUNDLESS",
  description:
    "Download MatrixX (Windows): Telegram fleet operations — discovery, group monitoring, member extraction, anti-ban broadcasting and AI auto-reply, deployed locally with data on-device. Includes step-by-step install and API-credential guide.",
  robots: { index: false, follow: false },
  alternates: {
    canonical: "/en/matrix/download",
    languages: { "zh-CN": "/matrix/download", en: "/en/matrix/download", "x-default": "/matrix/download" },
  },
  openGraph: {
    title: "Download MatrixX Client · BOUNDLESS",
    description: "Telegram fleet operations client: Windows available now, deployed locally with data on-device.",
    url: `${SITE_URL}/en/matrix/download`,
  },
};

const appLd = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: "MatrixX",
  applicationCategory: "BusinessApplication",
  operatingSystem: "Windows 10/11",
  softwareVersion: MATRIXX.download.version,
  offers: { "@type": "Offer", price: "0", priceCurrency: "USD", description: "Free download & trial" },
  publisher: { "@type": "Organization", name: "BOUNDLESS", url: SITE_URL },
};

export default function MatrixDownloadPageEn() {
  return (
    <main className="relative min-h-screen">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(appLd) }} />
      <Navbar />
      <MatrixxDownloadSection lang="en" />
      <Footer />
    </main>
  );
}
